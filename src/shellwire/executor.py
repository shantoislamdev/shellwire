# This file contains modified third-party code licensed under the MIT License. See NOTICE for details.
"""One-shot command execution with queue-based dispatch.

Commands run in their own process group (``os.setsid``) so that
``os.killpg`` can terminate the entire tree – shells, child processes,
and all.  A worker pool caps concurrent commands, with overflow
jobs queued in a bounded FIFO queue.

Robustness features:
- PGID caching for kill fallback when process exits before kill
- Process group liveness probing via signal 0
- Output overflow kills the process (prevents pipe-buffer deadlock)
- Grandchild-safe pipe drain (3 idle cycles after shell exit)
- Compound background command rewriting (A && B & trap fix)
- CWD and environment variable passthrough
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, Optional

from shellwire.config import DaemonConfig

logger = logging.getLogger(__name__)

# Type alias for the output callback.
OutputCallback = Callable[[str, str, str], Coroutine[Any, Any, None]]
# (command_id, data, stream_name)


class QueueFullError(Exception):
    """Raised when the command queue is full and cannot accept more jobs."""

    pass


@dataclass
class _QueuedJob:
    """Internal representation of a queued command job."""

    command_id: str
    command: str
    timeout: int
    on_output: Optional[OutputCallback]
    future: asyncio.Future
    cwd: str = ""
    env: Optional[Dict[str, str]] = None
    stdin_data: str = ""
    cancelled: bool = False


class CommandExecutor:
    """Command executor with parallel workers and background queuing.

    Uses N worker coroutines (N = max_concurrent_commands) that pull jobs
    from a bounded asyncio.Queue. When all workers are busy, new jobs are
    queued. When the queue is also full, QueueFullError is raised.

    Each command runs inside its own process group so the full tree can be
    killed cleanly via SIGTERM → SIGKILL escalation.
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._queue: asyncio.Queue[_QueuedJob] = asyncio.Queue(
            maxsize=config.max_queue_size
        )
        self._active: Dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set = set()
        self._workers: list = [
            asyncio.ensure_future(self._worker_loop())
            for _ in range(config.max_concurrent_commands)
        ]

    @property
    def active_count(self) -> int:
        """Number of commands currently running."""
        return len(self._active)

    @property
    def queued_count(self) -> int:
        """Number of commands waiting in the queue."""
        return self._queue.qsize()

    def get_queue_position(self, command_id: str) -> Optional[int]:
        """Return the 1-based position of the command in the queue, or None if not queued."""
        for i, job in enumerate(self._queue._queue):
            if job.command_id == command_id:
                return i + 1
        return None

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
        cwd: str = "",
        env: Optional[Dict[str, str]] = None,
        stdin_data: str = "",
    ) -> Dict[str, Any]:
        """Execute *command* and return its result.

        If all workers are busy, the job is queued. If the queue is also
        full, QueueFullError is raised.

        Args:
            command_id: Unique identifier for this invocation.
            command: Shell command string.
            timeout: Maximum wall-clock seconds (falls back to config default).
            on_output: Async callback invoked for each chunk of stdout/stderr.
            cwd: Working directory for the command (empty = inherit).
            env: Extra environment variables to merge into subprocess env.
            stdin_data: Data to pipe to stdin before execution.

        Returns:
            A dict with ``exit_code`` and ``duration_ms``.

        Raises:
            QueueFullError: If all workers busy and queue is full.
        """
        effective_timeout = timeout if timeout is not None else self._config.default_timeout

        # Create a future that will be resolved by the worker
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        job = _QueuedJob(
            command_id=command_id,
            command=command,
            timeout=effective_timeout,
            on_output=on_output,
            future=future,
            cwd=cwd,
            env=env,
            stdin_data=stdin_data,
        )

        # Try to enqueue the job
        try:
            self._queue.put_nowait(job)
            logger.debug(
                "Enqueued command %s (queue size: %d)",
                command_id,
                self._queue.qsize(),
            )
        except asyncio.QueueFull:
            raise QueueFullError(
                f"Queue full ({self._config.max_queue_size} pending jobs)"
            )

        # Wait for the worker to complete the job
        return await future

    async def cancel(self, command_id: str) -> bool:
        """Cancel a running or queued command by its ID.

        Returns:
            ``True`` if the command was found and cancelled.
        """
        # Check if it's currently running
        proc = self._active.get(command_id)
        if proc is not None:
            self._cancelled.add(command_id)
            await self._kill_process_tree(proc)
            return True

        # Check if it's in the queue (not yet started)
        # We can't remove from asyncio.Queue easily, so mark it as cancelled
        # and the worker will skip it when it picks it up
        for job in list(self._queue._queue):  # Access internal deque
            if job.command_id == command_id:
                job.cancelled = True
                if not job.future.done():
                    job.future.cancel()
                logger.debug("Cancelled queued command %s", command_id)
                return True

        return False

    async def wait_for_completion(self, timeout: float) -> int:
        """Wait for active commands and queue to drain, return count still running.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            Number of commands still running after timeout.
        """
        deadline = time.monotonic() + timeout
        while (self._active or not self._queue.empty()) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return len(self._active)

    def shutdown(self) -> None:
        """Cancel all worker tasks. Called during graceful shutdown."""
        for worker in self._workers:
            worker.cancel()
        logger.debug("Cancelled %d worker tasks", len(self._workers))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        """Worker coroutine that pulls jobs from the queue and executes them."""
        try:
            while True:
                job = await self._queue.get()
                try:
                    # Skip if cancelled while in queue
                    if job.cancelled:
                        logger.debug("Skipping cancelled job %s", job.command_id)
                        if not job.future.done():
                            job.future.cancel()
                        continue

                    # Execute the command
                    result = await self._run(
                        job.command_id,
                        job.command,
                        job.timeout,
                        job.on_output,
                        cwd=job.cwd,
                        env=job.env,
                        stdin_data=job.stdin_data,
                    )
                    if not job.future.done():
                        job.future.set_result(result)
                except Exception as exc:
                    logger.error(
                        "Worker error executing command %s",
                        job.command_id,
                        exc_info=True,
                    )
                    if not job.future.done():
                        job.future.set_exception(exc)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.debug("Worker loop cancelled")
            raise

    async def _run(
        self,
        command_id: str,
        command: str,
        timeout: int,
        on_output: Optional[OutputCallback],
        *,
        cwd: str = "",
        env: Optional[Dict[str, str]] = None,
        stdin_data: str = "",
    ) -> Dict[str, Any]:
        """Core execution loop with robustness patterns."""
        start = time.monotonic()

        # Apply compound background rewriting
        if self._config.rewrite_compound_background:
            try:
                from shellwire.shell_rewrite import rewrite_compound_background

                command = rewrite_compound_background(command)
            except Exception:
                logger.debug("Shell rewrite failed, using original command", exc_info=True)

        # Build subprocess environment
        proc_env = None
        if env:
            proc_env = {**os.environ, **env}

        # Resolve CWD (empty string = don't specify, inherit daemon's cwd)
        proc_cwd = cwd if cwd else None

        # Determine stdin mode
        use_stdin_pipe = bool(stdin_data)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if use_stdin_pipe else asyncio.subprocess.DEVNULL,
                preexec_fn=os.setsid,
                cwd=proc_cwd,
                env=proc_env,
            )
        except OSError as exc:
            logger.error("Failed to spawn command '%s': %s", command, exc)
            return {
                "exit_code": -1,
                "duration_ms": round((time.monotonic() - start) * 1000, 2),
                "error": str(exc),
            }

        self._active[command_id] = proc

        # Cache PGID immediately for kill fallback.
        # If process exits before _kill_process_tree, os.getpgid(pid) fails
        # with ProcessLookupError. The cached PGID is the fallback.
        try:
            proc._shellwire_pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined]
        except (ProcessLookupError, OSError):
            proc._shellwire_pgid = None  # type: ignore[attr-defined]

        # Pipe stdin data if provided
        if use_stdin_pipe and stdin_data and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_data.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, OSError, ConnectionResetError):
                logger.debug("Stdin pipe error for %s", command_id)

        total_output_size = 0
        output_overflow = False

        async def _stream_pipe(
            pipe: asyncio.StreamReader, stream_name: str
        ) -> None:
            """Stream pipe with grandchild-safe drain.

            After the shell process exits, allows 3 more idle read cycles
            (~1.5s) before stopping — prevents indefinite hangs from
            backgrounded grandchild processes that inherited the pipe.
            """
            nonlocal total_output_size, output_overflow
            idle_after_exit = 0

            while True:
                # Use per-read timeout to avoid indefinite blocking on
                # grandchild processes that inherit the pipe.
                try:
                    chunk = await asyncio.wait_for(pipe.read(4096), timeout=0.5)
                except asyncio.TimeoutError:
                    # Pipe read timed out — check if shell has exited.
                    if proc.returncode is not None:
                        idle_after_exit += 1
                        if idle_after_exit >= 3:
                            # Process dead + pipe idle for 3 cycles → stop
                            # waiting on grandchild output.
                            break
                    continue

                if not chunk:
                    break

                total_output_size += len(chunk)

                # Output overflow kills the process.
                # Without this, the process keeps running, fills the pipe
                # buffer, and eventually blocks forever.
                if total_output_size > self._config.max_output_size:
                    if not output_overflow:
                        output_overflow = True
                        logger.warning(
                            "Command %s exceeded max output size (%d bytes), killing",
                            command_id,
                            self._config.max_output_size,
                        )
                        await self._kill_process_tree(proc)
                        if on_output is not None:
                            try:
                                await on_output(
                                    command_id,
                                    f"\n[shellwire] Output truncated at "
                                    f"{self._config.max_output_size} bytes\n",
                                    "stderr",
                                )
                            except Exception:
                                pass
                    return

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

        Enhancements for killing processes:
        - PGID caching fallback (proc._shellwire_pgid)
        - Process group liveness probe via signal 0
        """
        if proc.returncode is not None:
            return  # Already exited.

        pid = proc.pid
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            # Process already gone — try cached PGID as fallback.
            pgid = getattr(proc, "_shellwire_pgid", None)
            if pgid is None:
                return

        # SIGTERM the whole group.
        try:
            os.killpg(pgid, signal.SIGTERM)
            logger.debug("Sent SIGTERM to process group %d", pgid)
        except (ProcessLookupError, OSError):
            return

        # Give it 3 seconds to exit gracefully.
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            # Process leader exited — check if entire group is dead.
            if not self._is_group_alive(pgid):
                return
        except asyncio.TimeoutError:
            pass

        # Escalate to SIGKILL.
        try:
            os.killpg(pgid, signal.SIGKILL)
            logger.debug("Sent SIGKILL to process group %d", pgid)
        except (ProcessLookupError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.error("Process group %d did not die after SIGKILL", pgid)

    @staticmethod
    def _is_group_alive(pgid: int) -> bool:
        """Probe whether any process in the group is still alive.

        Uses signal 0 (no-op signal) via ``os.killpg`` — the kernel
        checks permissions and existence without delivering a signal.

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
