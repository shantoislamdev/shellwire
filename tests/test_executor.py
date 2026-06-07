import asyncio
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shellwire.config import DaemonConfig
from shellwire.executor import CommandExecutor

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
        mock_create.assert_called_once_with(
            "echo hello",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid
        )

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
        
        async def slow_read(*args, **kwargs):
            await asyncio.sleep(5)
            return b""
            
        mock_proc.stdout.read = AsyncMock(side_effect=slow_read)
        mock_proc.stderr.read = AsyncMock(side_effect=slow_read)
        # To simulate a running process that waits forever until cancelled
        async def slow_wait():
            await asyncio.sleep(5)
            
        mock_proc.wait = AsyncMock(side_effect=slow_wait)
        
        mock_create.return_value = mock_proc
        
        # Start command in background
        task = asyncio.create_task(executor.execute("cmd-2", "sleep 100"))
        
        # Yield to event loop to let it start
        await asyncio.sleep(0.01)
        
        assert executor.active_count == 1
        
        # Cancel the command
        success = await executor.cancel("cmd-2")
        assert success is True
        
        mock_killpg.assert_called_with(1234, signal.SIGKILL)
        
        # Wait for task to finish
        await task
