import asyncio
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shellwire.config import DaemonConfig
from shellwire.session import SessionManager

# Mock posix functions for windows tests
if not hasattr(os, "setsid"):
    os.setsid = MagicMock()
if not hasattr(os, "getpgid"):
    os.getpgid = MagicMock(return_value=444)
if not hasattr(os, "killpg"):
    os.killpg = MagicMock()
if not hasattr(signal, "SIGKILL"):
    signal.SIGKILL = 9


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
        assert session._is_pty is False
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
            assert not any(s["id"] == "client-1" for s in manager.list_sessions())


@pytest.mark.asyncio
async def test_session_close_stdin():
    """Test closing stdin (sending EOF) to a session."""
    config = DaemonConfig()
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        await manager.start_session("client-1", "cat")
        
        # Send data + close stdin in one call
        await manager.send_input("client-1", "hello", close_stdin=True)
        mock_proc.stdin.write.assert_called_with(b"hello")
        mock_proc.stdin.close.assert_called_once()


@pytest.mark.asyncio
async def test_session_max_limit():
    """Maximum sessions limit is enforced."""
    config = DaemonConfig(max_sessions=2)
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_create.return_value = mock_proc
        
        await manager.start_session("s-1", "bash")
        await manager.start_session("s-2", "bash")
        
        with pytest.raises(ValueError, match="Maximum sessions"):
            await manager.start_session("s-3", "bash")


@pytest.mark.asyncio
async def test_session_duplicate_id():
    """Duplicate session IDs are rejected."""
    config = DaemonConfig()
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_create.return_value = mock_proc
        
        await manager.start_session("s-1", "bash")
        
        with pytest.raises(ValueError, match="already exists"):
            await manager.start_session("s-1", "bash")


@pytest.mark.asyncio
async def test_resize_non_pty_raises():
    """Resizing a non-PTY session raises RuntimeError."""
    config = DaemonConfig()
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_create.return_value = mock_proc
        
        await manager.start_session("s-1", "bash")
        
        with pytest.raises(RuntimeError, match="not a PTY"):
            await manager.resize_session("s-1", 120, 40)


@pytest.mark.asyncio
async def test_list_sessions_includes_pty_flag():
    """list_sessions includes is_pty flag."""
    config = DaemonConfig()
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_create.return_value = mock_proc
        
        await manager.start_session("s-1", "bash")
        
        sessions = manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["is_pty"] is False
        assert sessions[0]["id"] == "s-1"


@pytest.mark.asyncio
async def test_kill_all():
    """kill_all terminates all sessions."""
    config = DaemonConfig()
    manager = SessionManager(config)
    
    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create, \
         patch("os.killpg"), patch("os.getpgid", return_value=444):
        mock_proc = AsyncMock()
        mock_proc.pid = 444
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc
        
        await manager.start_session("s-1", "bash")
        await manager.start_session("s-2", "bash")
        
        assert manager.active_count == 2
        
        await manager.kill_all()
        
        assert manager.active_count == 0


@pytest.mark.asyncio
async def test_group_liveness_probe():
    """_is_group_alive correctly probes process group existence."""
    with patch("os.killpg") as mock_killpg:
        # Group alive
        mock_killpg.return_value = None
        assert SessionManager._is_group_alive(1234) is True
        
        # Group dead
        mock_killpg.side_effect = ProcessLookupError
        assert SessionManager._is_group_alive(1234) is False
        
        # Permission error (exists)
        mock_killpg.side_effect = PermissionError
        assert SessionManager._is_group_alive(1234) is True
