import asyncio
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shellwire.config import DaemonConfig
from shellwire.executor import CommandExecutor, QueueFullError

# Mock posix functions for windows tests
if not hasattr(os, "setsid"):
    os.setsid = MagicMock()
if not hasattr(os, "getpgid"):
    os.getpgid = MagicMock()
if not hasattr(os, "killpg"):
    os.killpg = MagicMock()
if not hasattr(signal, "SIGKILL"):
    signal.SIGKILL = 9

@pytest.mark.asyncio
async def test_execute_success():
    config = DaemonConfig()
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1000
        mock_proc.stdout.read = AsyncMock(side_effect=[b"hello", b""])
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        output_chunks = []
        async def on_output(cmd_id, text, stream):
            output_chunks.append((text, stream))
            
        result = await executor.execute("cmd-1", "echo hello", on_output=on_output)
        
        assert result["exit_code"] == 0
        assert ("hello", "stdout") in output_chunks
        mock_create.assert_called_once()
        # Verify key kwargs are passed
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs.get("preexec_fn") == os.setsid or call_kwargs[1].get("preexec_fn") == os.setsid
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_execute_with_cwd_and_env():
    """Verify CWD and environment variables are passed through."""
    config = DaemonConfig()
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1000
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        result = await executor.execute(
            "cmd-1", "ls",
            cwd="/tmp",
            env={"FOO": "bar"},
        )
        
        assert result["exit_code"] == 0
        call_kwargs = mock_create.call_args
        assert call_kwargs[1].get("cwd") == "/tmp"
        assert "FOO" in call_kwargs[1].get("env", {})
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_pgid_caching_fallback():
    """PGID is cached on the process object for kill fallback."""
    config = DaemonConfig()
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create, \
         patch("os.getpgid", return_value=5678) as mock_getpgid:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1234
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        result = await executor.execute("cmd-1", "echo hi")
        
        # PGID should have been cached
        assert hasattr(mock_proc, '_shellwire_pgid')
        assert mock_proc._shellwire_pgid == 5678
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_output_overflow_kills_process():
    """When output exceeds max_output_size, the process is killed."""
    config = DaemonConfig(max_output_size=100)  # Very small limit
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create, \
         patch("os.getpgid", return_value=1234), \
         patch("os.killpg") as mock_killpg:
        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.returncode = None
        
        # Generate output exceeding the limit
        large_output = b"x" * 200
        mock_proc.stdout.read = AsyncMock(side_effect=[large_output, b""])
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        overflow_notified = []
        async def on_output(cmd_id, text, stream):
            if "truncated" in text.lower():
                overflow_notified.append(True)
        
        result = await executor.execute("cmd-1", "cat /dev/urandom", on_output=on_output)
        
        # Process should have been killed
        mock_killpg.assert_called()
        # Client should have been notified about truncation
        assert len(overflow_notified) > 0
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_group_liveness_probe():
    """_is_group_alive correctly probes process group existence."""
    executor = CommandExecutor(DaemonConfig())
    
    with patch("os.killpg") as mock_killpg:
        # Group alive — killpg succeeds
        mock_killpg.return_value = None
        assert executor._is_group_alive(1234) is True
        mock_killpg.assert_called_with(1234, 0)
        
        # Group dead — ProcessLookupError
        mock_killpg.side_effect = ProcessLookupError
        assert executor._is_group_alive(1234) is False
        
        # Permission error — exists but can't signal
        mock_killpg.side_effect = PermissionError
        assert executor._is_group_alive(1234) is True
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_cancel():
    config = DaemonConfig()
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create, \
         patch("os.getpgid", return_value=1234) as mock_getpgid, \
         patch("os.killpg") as mock_killpg:
         
        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.returncode = None
        # stdin is DEVNULL (no stdin_data), so no stdin mock needed
        
        # Use a long-running read that can be cancelled
        read_event = asyncio.Event()
        async def slow_read(*args, **kwargs):
            try:
                await asyncio.wait_for(read_event.wait(), timeout=30)
            except asyncio.CancelledError:
                raise
            return b""
            
        mock_proc.stdout.read = AsyncMock(side_effect=slow_read)
        mock_proc.stderr.read = AsyncMock(side_effect=slow_read)
        
        async def slow_wait():
            await asyncio.wait_for(read_event.wait(), timeout=30)
            
        mock_proc.wait = AsyncMock(side_effect=slow_wait)
        
        mock_create.return_value = mock_proc
        
        # Start command in background
        task = asyncio.create_task(executor.execute("cmd-2", "sleep 100"))
        
        # Yield to event loop to let it start
        await asyncio.sleep(0.05)
        
        assert executor.active_count == 1
        
        # Cancel the command
        success = await executor.cancel("cmd-2")
        assert success is True
        
        # Process should have been signalled
        mock_killpg.assert_called()
        
        # Unblock reads so the task can finish cleanly
        read_event.set()
        mock_proc.returncode = -9  # killed
        
        # Wait for task to finish
        await task
    
    executor.shutdown()


# ---------------------------------------------------------------------------
# Queue tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_when_busy():
    """When all workers are busy, jobs are queued and run when a slot opens."""
    config = DaemonConfig(max_concurrent_commands=2, max_queue_size=4)
    executor = CommandExecutor(config)
    
    call_order = []
    
    async def make_mock_proc(cmd_id, delay=0.1):
        """Create a mock process that simulates work."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1000 + len(call_order)
        
        async def delayed_read(*args, **kwargs):
            call_order.append(f"start-{cmd_id}")
            await asyncio.sleep(delay)
            call_order.append(f"end-{cmd_id}")
            return b""
        
        mock_proc.stdout.read = AsyncMock(side_effect=delayed_read)
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        return mock_proc
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        # Create 3 different mock processes
        procs = [
            await make_mock_proc("cmd-1", delay=0.2),
            await make_mock_proc("cmd-2", delay=0.2),
            await make_mock_proc("cmd-3", delay=0.1),
        ]
        mock_create.side_effect = procs
        
        # Start 2 commands (fills all workers)
        task1 = asyncio.create_task(executor.execute("cmd-1", "echo 1"))
        task2 = asyncio.create_task(executor.execute("cmd-2", "echo 2"))
        
        # Give workers time to pick up jobs
        await asyncio.sleep(0.05)
        
        # Start 3rd command (should be queued)
        task3 = asyncio.create_task(executor.execute("cmd-3", "echo 3"))
        await asyncio.sleep(0.01)
        
        # Verify queue state
        assert executor.active_count == 2
        assert executor.queued_count == 1
        
        # Wait for all to complete
        await asyncio.gather(task1, task2, task3)
        
        # Verify all commands ran
        assert executor.active_count == 0
        assert executor.queued_count == 0
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_queue_full_rejects():
    """When queue is full, QueueFullError is raised."""
    config = DaemonConfig(max_concurrent_commands=1, max_queue_size=1)
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1000
        
        async def slow_read(*args, **kwargs):
            await asyncio.sleep(1)
            return b""
        
        mock_proc.stdout.read = AsyncMock(side_effect=slow_read)
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        # Start 1 command (fills worker)
        task1 = asyncio.create_task(executor.execute("cmd-1", "echo 1"))
        await asyncio.sleep(0.01)
        
        # Start 2nd command (fills queue)
        task2 = asyncio.create_task(executor.execute("cmd-2", "echo 2"))
        await asyncio.sleep(0.01)
        
        # Try to start 3rd command (should fail)
        with pytest.raises(QueueFullError) as exc_info:
            await executor.execute("cmd-3", "echo 3")
        
        assert "Queue full" in str(exc_info.value)
        
        # Clean up
        task1.cancel()
        task2.cancel()
        try:
            await task1
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await task2
        except (asyncio.CancelledError, Exception):
            pass
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_cancel_queued():
    """Can cancel a command that's still in the queue."""
    config = DaemonConfig(max_concurrent_commands=1, max_queue_size=4)
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1000
        
        async def slow_read(*args, **kwargs):
            await asyncio.sleep(1)
            return b""
        
        mock_proc.stdout.read = AsyncMock(side_effect=slow_read)
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        # Start 1 command (fills worker)
        task1 = asyncio.create_task(executor.execute("cmd-1", "echo 1"))
        await asyncio.sleep(0.01)
        
        # Start 2nd command (goes to queue)
        task2 = asyncio.create_task(executor.execute("cmd-2", "echo 2"))
        await asyncio.sleep(0.01)
        
        assert executor.queued_count == 1
        
        # Cancel the queued command
        success = await executor.cancel("cmd-2")
        assert success is True
        
        # Clean up
        task1.cancel()
        try:
            await task1
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await task2
        except (asyncio.CancelledError, Exception):
            pass
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_queue_fifo_order():
    """Queued jobs execute in FIFO order."""
    config = DaemonConfig(max_concurrent_commands=1, max_queue_size=4)
    executor = CommandExecutor(config)
    
    execution_order = []
    
    async def make_mock_proc(cmd_id):
        """Create a mock process that records execution order."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1000 + len(execution_order)
        
        async def record_read(*args, **kwargs):
            execution_order.append(cmd_id)
            await asyncio.sleep(0.05)
            return b""
        
        mock_proc.stdout.read = AsyncMock(side_effect=record_read)
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        return mock_proc
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        # Create 4 mock processes
        procs = [
            await make_mock_proc("cmd-1"),
            await make_mock_proc("cmd-2"),
            await make_mock_proc("cmd-3"),
            await make_mock_proc("cmd-4"),
        ]
        mock_create.side_effect = procs
        
        # Start all commands
        task1 = asyncio.create_task(executor.execute("cmd-1", "echo 1"))
        await asyncio.sleep(0.01)
        task2 = asyncio.create_task(executor.execute("cmd-2", "echo 2"))
        task3 = asyncio.create_task(executor.execute("cmd-3", "echo 3"))
        task4 = asyncio.create_task(executor.execute("cmd-4", "echo 4"))
        
        # Wait for all to complete
        await asyncio.gather(task1, task2, task3, task4)
        
        # Verify FIFO order
        assert execution_order == ["cmd-1", "cmd-2", "cmd-3", "cmd-4"]
    
    executor.shutdown()


@pytest.mark.asyncio
async def test_execute_with_stdin():
    """Verify stdin data is piped to the process."""
    config = DaemonConfig()
    executor = CommandExecutor(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.pid = 1000
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdin.close = MagicMock()
        mock_create.return_value = mock_proc
        
        result = await executor.execute(
            "cmd-1", "cat",
            stdin_data="hello world",
        )
        
        assert result["exit_code"] == 0
        # stdin should have been created as PIPE
        call_kwargs = mock_create.call_args
        assert call_kwargs[1].get("stdin") == asyncio.subprocess.PIPE
        # Data should have been written
        mock_proc.stdin.write.assert_called_once_with(b"hello world")
    
    executor.shutdown()
