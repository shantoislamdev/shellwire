"""Long-running interactive session manager.

Sessions are persistent shell processes that survive beyond a single
``execute`` call.  They support stdin input, background output streaming,
and clean process-group termination.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from kotha_shell.config import DaemonConfig

logger = logging.getLogger(__name__)

# Type alias for the output callback.
SessionOutputCallback = Callable[[str, str, str], Coroutine[Any, Any, None]]
# (session_id, data, stream_name)


@dataclass
class Session:
    """A running interactive session."""

    id: str
    process: asyncio.subprocess.Process
    pgid: int
    started_at: float
    command: str
    output_buffer: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=500)
    )
    _stream_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _on_output: Optional[SessionOutputCallback] = field(
        default=None, repr=False
    )


class SessionManager:
    """Manage long-running interactive sessions.

    Each session is a subprocess in its own process group with streaming
    stdout/stderr and stdin input support.
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._sessions: Dict[str, Session] = {}

    @property
    def active_count(self) -> int:
        """Number of currently active sessions."""
        return len(self._sessions)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_session(
        self,
        session_id: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        on_output: Optional[SessionOutputCallback] = None,
    ) -> Session:
        """Start a new interactive session.

        Args:
            session_id: Unique identifier for this session.
            command: Shell command to run.
            cwd: Working directory (defaults to ``$HOME``).
            on_output: Async callback invoked for each chunk of output.

        Returns:
            The :class:`Session` object.

        Raises:
            ValueError: If a session with *session_id* already exists or
                the maximum number of sessions has been reached.
        """
        if session_id in self._sessions:
            raise ValueError(f"Session '{session_id}' already exists")

        if len(self._sessions) >= self._config.max_sessions:
            raise ValueError(
                f"Maximum sessions ({self._config.max_sessions}) reached"
            )

        work_dir = cwd or os.environ.get("HOME", "/")

        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            preexec_fn=os.setsid,
        )

        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid

        session = Session(
            id=session_id,
            process=proc,
            pgid=pgid,
            started_at=time.time(),
            command=command,
            _on_output=on_output,
        )
        self._sessions[session_id] = session

        # Start background streaming task.
        session._stream_task = asyncio.ensure_future(
            self._stream_output(session)
        )

        logger.info(
            "Session started: id=%s pid=%d command='%s'",
            session_id,
            proc.pid,
            command,
        )
        return session

    async def send_input(self, session_id: str, data: str) -> None:
        """Send data to the stdin of a running session.

        Args:
            session_id: Target session.
            data: Text to write to stdin.

        Raises:
            KeyError: If no session with *session_id* exists.
            RuntimeError: If stdin is not available.
        """
        session = self._get_session(session_id)
        stdin = session.process.stdin
        if stdin is None:
            raise RuntimeError(f"Session '{session_id}' has no stdin")

        encoded = data.encode("utf-8")
        stdin.write(encoded)
        await stdin.drain()
        logger.debug("Sent %d bytes to session %s stdin", len(encoded), session_id)

    async def kill_session(self, session_id: str) -> Dict[str, Any]:
        """Kill a session's process group and return a summary.

        Args:
            session_id: The session to terminate.

        Returns:
            A dict with ``exit_code`` and ``duration_ms``.

        Raises:
            KeyError: If no session with *session_id* exists.
        """
        session = self._get_session(session_id)
        await self._kill_process_group(session)

        # Wait for the stream task to finish.
        if session._stream_task is not None and not session._stream_task.done():
            session._stream_task.cancel()
            try:
                await session._stream_task
            except (asyncio.CancelledError, Exception):
                pass

        exit_code = (
            session.process.returncode
            if session.process.returncode is not None
            else -1
        )
        duration_ms = round((time.time() - session.started_at) * 1000, 2)

        self._sessions.pop(session_id, None)
        logger.info("Session killed: id=%s exit_code=%s", session_id, exit_code)

        return {"exit_code": exit_code, "duration_ms": duration_ms}

    def list_sessions(self) -> List[Dict[str, Any]]:
        """Return metadata for all active sessions.

        Returns:
            A list of dicts describing each session.
        """
        result: List[Dict[str, Any]] = []
        for sid, sess in self._sessions.items():
            result.append(
                {
                    "id": sid,
                    "pid": sess.process.pid,
                    "command": sess.command,
                    "started_at": sess.started_at,
                    "uptime_seconds": round(time.time() - sess.started_at, 1),
                    "is_running": sess.process.returncode is None,
                    "recent_output": list(sess.output_buffer)[-10:],
                }
            )
        return result

    async def kill_all(self) -> None:
        """Shutdown all sessions.  Called during daemon shutdown."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            try:
                await self.kill_session(sid)
            except Exception:
                logger.error("Error killing session %s", sid, exc_info=True)
        logger.info("All sessions killed")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> Session:
        """Retrieve a session or raise ``KeyError``."""
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"No such session: '{session_id}'")

    async def _stream_output(self, session: Session) -> None:
        """Background task that reads stdout/stderr and forwards output."""

        async def _read_pipe(
            pipe: asyncio.StreamReader, stream_name: str
        ) -> None:
            while True:
                chunk = await pipe.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                session.output_buffer.append(f"[{stream_name}] {text}")
                if session._on_output is not None:
                    try:
                        await session._on_output(session.id, text, stream_name)
                    except Exception:
                        logger.debug(
                            "Session output callback error", exc_info=True
                        )

        try:
            assert session.process.stdout is not None
            assert session.process.stderr is not None
            await asyncio.gather(
                _read_pipe(session.process.stdout, "stdout"),
                _read_pipe(session.process.stderr, "stderr"),
            )
            # Process ended naturally – clean up.
            await session.process.wait()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.error(
                "Error streaming session %s", session.id, exc_info=True
            )
        finally:
            # Notify that session ended.
            if session._on_output is not None:
                exit_code = session.process.returncode
                duration_ms = round(
                    (time.time() - session.started_at) * 1000, 2
                )
                try:
                    # Use a sentinel message to signal end.
                    await session._on_output(
                        session.id,
                        "",  # empty data signals end
                        f"__ended__:{exit_code}:{duration_ms}",
                    )
                except Exception:
                    pass

            # Remove from active sessions if still present.
            self._sessions.pop(session.id, None)

    async def _kill_process_group(self, session: Session) -> None:
        """SIGTERM → wait 3s → SIGKILL the process group."""
        proc = session.process
        if proc.returncode is not None:
            return

        try:
            os.killpg(session.pgid, signal.SIGTERM)
            logger.debug("Sent SIGTERM to session pgid %d", session.pgid)
        except OSError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass

        try:
            os.killpg(session.pgid, signal.SIGKILL)
            logger.debug("Sent SIGKILL to session pgid %d", session.pgid)
        except OSError:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.error(
                "Session pgid %d did not die after SIGKILL", session.pgid
            )
