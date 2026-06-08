"""One-shot command execution with process-group kill.

Commands run in their own process group (``os.setsid``) so that
``os.killpg`` can terminate the entire tree – shells, child processes,
and all.  A semaphore caps concurrent commands.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Any, Callable, Coroutine, Dict, Optional

from shellwire.config import DaemonConfig

logger = logging.getLogger(__name__)

# Type alias for the output callback.
OutputCallback = Callable[[str, str, str], Coroutine[Any, Any, None]]
# (command_id, data, stream_name)


class CommandExecutor:
    """Execute one-shot shell commands with concurrency limiting.

    Each command runs inside its own process group so the full tree can be
    killed cleanly via SIGTERM → SIGKILL escalation.
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_commands)
        self._active: Dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set = set()

    @property
    def active_count(self) -> int:
        """Number of commands currently running."""
        return len(self._active)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        command_id: str,
        command: str,
        *,
        timeout: Optional[int] = None,
        on_output: Optional[OutputCallback] = None,
    ) -> Dict[str, Any]:
        """Execute *command* and return its result.

        Args:
            command_id: Unique identifier for this invocation.
            command: Shell command string.
            timeout: Maximum wall-clock seconds (falls back to config default).
            on_output: Async callback invoked for each chunk of stdout/stderr.

        Returns:
            A dict with ``exit_code`` and ``duration_ms``.
        """
        effective_timeout = timeout if timeout is not None else self._config.default_timeout

        async with self._semaphore:
            return await self._run(command_id, command, effective_timeout, on_output)

    async def cancel(self, command_id: str) -> bool:
        """Cancel a running command by its ID.

        Returns:
            ``True`` if the command was found and killed.
        """
        proc = self._active.get(command_id)
        if proc is None:
            return False

        self._cancelled.add(command_id)
        await self._kill_process_tree(proc)
        return True

    async def wait_for_completion(self, timeout: float) -> int:
        """Wait for active commands to finish, return count still running.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            Number of commands still running after timeout.
        """
        deadline = time.monotonic() + timeout
        while self._active and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return len(self._active)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(
        self,
        command_id: str,
        command: str,
        timeout: int,
        on_output: Optional[OutputCallback],
    ) -> Dict[str, Any]:
        """Core execution loop."""
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid,
            )
        except OSError as exc:
            logger.error("Failed to spawn command '%s': %s", command, exc)
            return {
                "exit_code": -1,
                "duration_ms": round((time.monotonic() - start) * 1000, 2),
                "error": str(exc),
            }

        self._active[command_id] = proc
        total_output_size = 0

        async def _stream_pipe(
            pipe: asyncio.StreamReader, stream_name: str
        ) -> None:
            nonlocal total_output_size
            while True:
                chunk = await pipe.read(4096)
                if not chunk:
                    break
                total_output_size += len(chunk)
                if total_output_size > self._config.max_output_size:
                    logger.warning(
                        "Command %s exceeded max output size", command_id
                    )
                    break
                if on_output is not None:
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                        await on_output(command_id, text, stream_name)
                    except Exception:
                        logger.debug("Output callback error", exc_info=True)

        try:
            assert proc.stdout is not None
            assert proc.stderr is not None

            stdout_task = asyncio.ensure_future(
                _stream_pipe(proc.stdout, "stdout")
            )
            stderr_task = asyncio.ensure_future(
                _stream_pipe(proc.stderr, "stderr")
            )

            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task),
                    timeout=timeout,
                )
                await proc.wait()
            except asyncio.TimeoutError:
                logger.warning(
                    "Command %s timed out after %ds", command_id, timeout
                )
                await self._kill_process_tree(proc)
                stdout_task.cancel()
                stderr_task.cancel()
                # Notify the client about the timeout.
                if on_output is not None:
                    try:
                        await on_output(
                            command_id,
                            f"\n[shellwire] Command timed out after {timeout}s\n",
                            "stderr",
                        )
                    except Exception:
                        pass
        finally:
            self._active.pop(command_id, None)
            self._cancelled.discard(command_id)

        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        exit_code = proc.returncode if proc.returncode is not None else -1

        return {"exit_code": exit_code, "duration_ms": elapsed_ms}

    async def _kill_process_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the process group: SIGTERM first, then SIGKILL after 3s.

        Uses ``os.killpg`` to hit the entire tree created via ``setsid``.
        """
        if proc.returncode is not None:
            return  # Already exited.

        pid = proc.pid
        try:
            pgid = os.getpgid(pid)
        except OSError:
            # Process already gone.
            return

        # SIGTERM the whole group.
        try:
            os.killpg(pgid, signal.SIGTERM)
            logger.debug("Sent SIGTERM to process group %d", pgid)
        except OSError:
            return

        # Give it 3 seconds to exit gracefully.
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass

        # Escalate to SIGKILL.
        try:
            os.killpg(pgid, signal.SIGKILL)
            logger.debug("Sent SIGKILL to process group %d", pgid)
        except OSError:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.error("Process group %d did not die after SIGKILL", pgid)
