# This file contains modified third-party code licensed under the MIT License. See NOTICE for details.
"""Long-running interactive session manager.

Sessions are persistent shell processes that survive beyond a single
``execute`` call.  They support stdin input, background output streaming,
and clean process-group termination.

Robustness features:
- PTY mode via ptyprocess
- Close-stdin / EOF support
- Process group liveness probing via signal 0
- PGID caching for kill fallback
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

from shellwire.config import DaemonConfig

logger = logging.getLogger(__name__)

# Type alias for the output callback.
SessionOutputCallback = Callable[[str, str, str], Coroutine[Any, Any, None]]
# (session_id, data, stream_name)


@dataclass
class Session:
    """A running interactive session."""

    id: str
    process: Optional[asyncio.subprocess.Process]
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
    # PTY bridge instance (None for pipe-based sessions).
    _pty_bridge: Any = field(default=None, repr=False)
    _is_pty: bool = field(default=False, repr=False)
    # Last time this session had user interaction (send_input / start).
    # Used by the idle timeout watcher to reap abandoned sessions.
    last_activity: float = field(default_factory=time.time)


class SessionManager:
    """Manage long-running interactive sessions.

    Each session is a subprocess in its own process group with streaming
    stdout/stderr and stdin input support.  Optionally spawned behind a
    pseudo-terminal (PTY) for interactive programs.
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._sessions: Dict[str, Session] = {}
        self._idle_timeout_task: Optional[asyncio.Task] = None

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
        use_pty: bool = False,
        cols: int = 80,
        rows: int = 24,
    ) -> Session:
        """Start a new interactive session.

        Args:
            session_id: Unique identifier for this session.
            command: Shell command to run.
            cwd: Working directory (defaults to ``$HOME``).
            on_output: Async callback invoked for each chunk of output.
            use_pty: If True, spawn behind a pseudo-terminal (requires ptyprocess).
            cols: Initial terminal width (PTY mode only).
            rows: Initial terminal height (PTY mode only).

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

        if use_pty and self._config.enable_pty:
            return await self._start_pty_session(
                session_id, command, cwd=cwd, on_output=on_output,
                cols=cols, rows=rows,
            )

        return await self._start_pipe_session(
            session_id, command, cwd=cwd, on_output=on_output,
        )

    async def send_input(
        self, session_id: str, data: str, *, close_stdin: bool = False
    ) -> None:
        """Send data to the stdin of a running session.

        Args:
            session_id: Target session.
            data: Text to write to stdin.
            close_stdin: If True, close stdin after writing (send EOF).

        Raises:
            KeyError: If no session with *session_id* exists.
            RuntimeError: If stdin is not available.
        """
        session = self._get_session(session_id)

        if session._is_pty:
            # PTY mode: write bytes to the PTY bridge.
            if session._pty_bridge is None:
                raise RuntimeError(f"Session '{session_id}' PTY bridge is closed")
            if data:
                session._pty_bridge.write(data.encode("utf-8"))
            # PTY stdin cannot be "closed" — EOF is Ctrl+D (0x04).
            if close_stdin:
                session._pty_bridge.write(b"\x04")
            logger.debug(
                "Sent %d bytes to PTY session %s", len(data), session_id
            )
            session.last_activity = time.time()
            return

        # Pipe mode.
        if session.process is None:
            raise RuntimeError(f"Session '{session_id}' has no process")
        stdin = session.process.stdin
        if stdin is None:
            raise RuntimeError(f"Session '{session_id}' has no stdin")

        if data:
            encoded = data.encode("utf-8")
            stdin.write(encoded)
            await stdin.drain()
            logger.debug(
                "Sent %d bytes to session %s stdin", len(encoded), session_id
            )

        if close_stdin:
            stdin.close()
            logger.debug("Closed stdin for session %s", session_id)

        session.last_activity = time.time()

    async def resize_session(
        self, session_id: str, cols: int, rows: int
    ) -> None:
        """Resize the PTY terminal of a running session.

        Args:
            session_id: Target session.
            cols: New terminal width.
            rows: New terminal height.

        Raises:
            KeyError: If no session with *session_id* exists.
            RuntimeError: If the session is not a PTY session.
        """
        session = self._get_session(session_id)
        if not session._is_pty or session._pty_bridge is None:
            raise RuntimeError(
                f"Session '{session_id}' is not a PTY session"
            )
        session._pty_bridge.resize(cols, rows)
        logger.debug(
            "Resized PTY session %s to %dx%d", session_id, cols, rows
        )

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

        if session._is_pty:
            await self._kill_pty_session(session)
        else:
            await self._kill_process_group(session)

        # Wait for the stream task to finish.
        if session._stream_task is not None and not session._stream_task.done():
            session._stream_task.cancel()
            try:
                await session._stream_task
            except (asyncio.CancelledError, Exception):
                pass

        exit_code: Optional[int] = -1
        if session.process is not None:
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
            pid = -1
            is_running = False
            if sess._is_pty and sess._pty_bridge is not None:
                pid = sess._pty_bridge.pid
                is_running = sess._pty_bridge.is_alive()
            elif sess.process is not None:
                pid = sess.process.pid
                is_running = sess.process.returncode is None

            result.append(
                {
                    "id": sid,
                    "pid": pid,
                    "command": sess.command,
                    "started_at": sess.started_at,
                    "uptime_seconds": round(time.time() - sess.started_at, 1),
                    "is_running": is_running,
                    "is_pty": sess._is_pty,
                    "recent_output": list(sess.output_buffer)[-10:],
                }
            )
        return result

    async def kill_all(self) -> None:
        """Shutdown all sessions.  Called during daemon shutdown."""
        self.stop_idle_watcher()
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            try:
                await self.kill_session(sid)
            except Exception:
                logger.error("Error killing session %s", sid, exc_info=True)
        logger.info("All sessions killed")

    # ------------------------------------------------------------------
    # Idle timeout watcher (adapted from Hermes gateway/run.py)
    # ------------------------------------------------------------------

    def start_idle_watcher(self) -> None:
        """Start the idle session timeout watcher task.

        On Termux/Android, idle sessions holding open process groups
        waste precious phantom-process-killer budget.  This watcher
        periodically scans for sessions that have had no user
        interaction for longer than ``session_idle_timeout`` and kills
        them.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._idle_timeout_task is not None:
            return
        timeout = self._config.session_idle_timeout
        if timeout <= 0:
            logger.info("Session idle timeout disabled (value=%s)", timeout)
            return
        self._idle_timeout_task = asyncio.ensure_future(
            self._idle_watcher_loop(timeout)
        )
        logger.info(
            "Session idle timeout watcher started (timeout=%ds)", timeout
        )

    def stop_idle_watcher(self) -> None:
        """Stop the idle session timeout watcher."""
        if self._idle_timeout_task is not None:
            self._idle_timeout_task.cancel()
            self._idle_timeout_task = None

    async def _idle_watcher_loop(self, timeout: float) -> None:
        """Background coroutine that reaps idle sessions.

        Scans every 60 seconds.  A session is considered idle when
        ``time.time() - session.last_activity > timeout``.
        """
        _SCAN_INTERVAL = 60  # seconds
        _MAX_WARNINGS = 3  # warn before killing
        warned: Dict[str, int] = {}  # session_id → warning count

        try:
            while True:
                await asyncio.sleep(_SCAN_INTERVAL)
                now = time.time()
                to_kill: List[str] = []

                for sid, session in list(self._sessions.items()):
                    idle_secs = now - session.last_activity
                    if idle_secs <= timeout:
                        warned.pop(sid, None)
                        continue

                    count = warned.get(sid, 0) + 1
                    warned[sid] = count

                    if count <= _MAX_WARNINGS:
                        logger.warning(
                            "Session %s idle for %ds (timeout=%ds, "
                            "warning %d/%d)",
                            sid,
                            int(idle_secs),
                            int(timeout),
                            count,
                            _MAX_WARNINGS,
                        )
                    else:
                        logger.warning(
                            "Session %s idle for %ds — killing "
                            "(exceeded %d warnings)",
                            sid,
                            int(idle_secs),
                            _MAX_WARNINGS,
                        )
                        to_kill.append(sid)

                for sid in to_kill:
                    try:
                        await self.kill_session(sid)
                        warned.pop(sid, None)
                        logger.info(
                            "Idle session %s killed by timeout watcher", sid
                        )
                    except Exception:
                        logger.error(
                            "Failed to kill idle session %s",
                            sid,
                            exc_info=True,
                        )

                # Clean up warnings for sessions that no longer exist.
                warned = {
                    sid: c
                    for sid, c in warned.items()
                    if sid in self._sessions
                }
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Pipe-based session (original path)
    # ------------------------------------------------------------------

    async def _start_pipe_session(
        self,
        session_id: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        on_output: Optional[SessionOutputCallback] = None,
    ) -> Session:
        """Start a pipe-based (non-PTY) session."""
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
            _is_pty=False,
        )
        self._sessions[session_id] = session

        # Start background streaming task.
        session._stream_task = asyncio.ensure_future(
            self._stream_pipe_output(session)
        )

        logger.info(
            "Session started (pipe): id=%s pid=%d command='%s'",
            session_id,
            proc.pid,
            command,
        )
        return session

    # ------------------------------------------------------------------
    # PTY-based session
    # ------------------------------------------------------------------

    async def _start_pty_session(
        self,
        session_id: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        on_output: Optional[SessionOutputCallback] = None,
        cols: int = 80,
        rows: int = 24,
    ) -> Session:
        """Start a PTY-based session using ptyprocess.

        Falls back to pipe-based session if PTY is unavailable.
        """
        try:
            from shellwire.pty_bridge import PtyBridge, PtyUnavailableError
        except ImportError:
            logger.warning(
                "pty_bridge not available, falling back to pipe session"
            )
            return await self._start_pipe_session(
                session_id, command, cwd=cwd, on_output=on_output,
            )

        work_dir = cwd or os.environ.get("HOME", "/")
        shell = self._config.shell_path or os.environ.get("SHELL", "/bin/sh")

        try:
            pty = PtyBridge.spawn(
                [shell, "-c", command],
                cwd=work_dir,
                cols=cols,
                rows=rows,
            )
        except PtyUnavailableError as exc:
            logger.warning("PTY unavailable: %s — falling back to pipe", exc)
            return await self._start_pipe_session(
                session_id, command, cwd=cwd, on_output=on_output,
            )
        except (OSError, FileNotFoundError) as exc:
            logger.error("Failed to spawn PTY session: %s", exc)
            raise ValueError(f"Failed to start PTY session: {exc}") from exc

        session = Session(
            id=session_id,
            process=None,  # No asyncio.subprocess.Process for PTY sessions
            pgid=pty.pid,
            started_at=time.time(),
            command=command,
            _on_output=on_output,
            _pty_bridge=pty,
            _is_pty=True,
        )
        self._sessions[session_id] = session

        # Start background PTY reader in an executor thread.
        session._stream_task = asyncio.ensure_future(
            self._stream_pty_output(session)
        )

        logger.info(
            "Session started (PTY): id=%s pid=%d command='%s' %dx%d",
            session_id,
            pty.pid,
            command,
            cols,
            rows,
        )
        return session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> Session:
        """Retrieve a session or raise ``KeyError``."""
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"No such session: '{session_id}'")

    async def _stream_pipe_output(self, session: Session) -> None:
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
            assert session.process is not None
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
                exit_code = (
                    session.process.returncode
                    if session.process is not None
                    else None
                )
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

    async def _stream_pty_output(self, session: Session) -> None:
        """Background task that reads PTY output via an executor thread."""
        loop = asyncio.get_event_loop()
        pty = session._pty_bridge

        try:
            while pty is not None and pty.is_alive():
                # Read in executor thread to avoid blocking the event loop.
                data = await loop.run_in_executor(None, pty.read, 0.2)
                if data is None:
                    # EOF — child exited.
                    break
                if data == b"":
                    # No data available within timeout — keep polling.
                    continue

                text = data.decode("utf-8", errors="replace")
                session.output_buffer.append(f"[pty] {text}")
                if session._on_output is not None:
                    try:
                        await session._on_output(session.id, text, "stdout")
                    except Exception:
                        logger.debug(
                            "PTY session output callback error", exc_info=True
                        )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.error(
                "Error streaming PTY session %s", session.id, exc_info=True
            )
        finally:
            # Notify that session ended.
            exit_code = None
            if session._on_output is not None:
                duration_ms = round(
                    (time.time() - session.started_at) * 1000, 2
                )
                try:
                    await session._on_output(
                        session.id,
                        "",
                        f"__ended__:{exit_code}:{duration_ms}",
                    )
                except Exception:
                    pass

            # Remove from active sessions if still present.
            self._sessions.pop(session.id, None)

    async def _kill_process_group(self, session: Session) -> None:
        """SIGTERM → wait 3s → SIGKILL the process group.

        Enhanced with group liveness probing.
        """
        proc = session.process
        if proc is None or proc.returncode is not None:
            return

        pgid = session.pgid

        try:
            os.killpg(pgid, signal.SIGTERM)
            logger.debug("Sent SIGTERM to session pgid %d", pgid)
        except (ProcessLookupError, OSError):
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            # Leader exited — check if entire group is dead.
            if not self._is_group_alive(pgid):
                return
        except asyncio.TimeoutError:
            pass

        try:
            os.killpg(pgid, signal.SIGKILL)
            logger.debug("Sent SIGKILL to session pgid %d", pgid)
        except (ProcessLookupError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.error(
                "Session pgid %d did not die after SIGKILL", pgid
            )

    async def _kill_pty_session(self, session: Session) -> None:
        """Close the PTY bridge (escalating signal chain)."""
        pty = session._pty_bridge
        if pty is None:
            return

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, pty.close)
        except Exception:
            logger.debug("Error closing PTY bridge", exc_info=True)

        session._pty_bridge = None

    @staticmethod
    def _is_group_alive(pgid: int) -> bool:
        """Probe whether any process in the group is still alive (signal 0).

        Checks if the process group is alive.
        """
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but we can't signal it
        except OSError:
            return False
