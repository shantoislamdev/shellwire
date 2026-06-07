import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shellwire.config import DaemonConfig
from shellwire.session import SessionManager


@pytest.mark.asyncio
async def test_session_creation():
    config = DaemonConfig()
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_create.return_value = mock_proc
        
        session = await manager.start_session("client-1", "echo hello")
        
        assert session is not None
        assert any(s["id"] == "client-1" for s in manager.list_sessions())
        mock_create.assert_called_once()

@pytest.mark.asyncio
async def test_session_input_and_close():
    config = DaemonConfig()
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_proc.stdin.write = AsyncMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        session = await manager.start_session("client-1", "bash")
        
        # Test input
        await manager.send_input("client-1", "ls\n")
        mock_proc.stdin.write.assert_called_with(b"ls\n")
        
        # Test close
        with patch("os.killpg") as mock_killpg, patch("os.getpgid", return_value=444):
            result = await manager.kill_session("client-1")
            assert result["exit_code"] in (-1, None)
            assert not any(s["session_id"] == "client-1" for s in manager.list_sessions())
