"""One-shot command execution with queue-based dispatch.

Commands run in their own process group (``os.setsid``) so that
``os.killpg`` can terminate the entire tree – shells, child processes,
and all.  A worker pool caps concurrent commands, with overflow
jobs queued in a bounded FIFO queue.
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
    cancelled: bool = False


class CommandExecutor:
    """Execute one-shot shell commands with worker pool and queue.

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
    ) -> Dict[str, Any]:
        """Execute *command* and return its result.

        If all workers are busy, the job is queued. If the queue is also
        full, QueueFullError is raised.

        Args:
            command_id: Unique identifier for this invocation.
            command: Shell command string.
            timeout: Maximum wall-clock seconds (falls back to config default).
            on_output: Async callback invoked for each chunk of stdout/stderr.

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
                        job.command_id, job.command, job.timeout, job.on_output
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
