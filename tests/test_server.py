import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets

from shellwire.config import DaemonConfig
from shellwire.server import ShellwireServer


@pytest.mark.asyncio
async def test_health_check_endpoint():
    config = DaemonConfig()
    server = ShellwireServer(config)
    
    mock_websocket = AsyncMock()
    mock_websocket.request.path = "/health"
    
    # Simulate health check from websockets process_request
    response = await server.health_check_handler(None, mock_websocket.request)
    
    assert response is not None
    assert response.status_code == 200
    assert b"\"status\": \"ok\"" in response.body

@pytest.mark.asyncio
async def test_invalid_auth():
    config = DaemonConfig()
    server = ShellwireServer(config)
    
    mock_websocket = AsyncMock()
    
    with patch("shellwire.server.validate_token", return_value=False):
        # Simulate websocket yielding an invalid auth message
        mock_websocket.recv.return_value = '{"type": "auth", "token": "bad", "client_id": "test"}'
        
        await server.handler(mock_websocket)
        
        mock_websocket.close.assert_called_with(4005, "Invalid token")


# ---------------------------------------------------------------------------
# Graceful shutdown tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_rejects_during_shutdown():
    """_dispatch sends DAEMON_STOPPING error when _shutting_down is True."""
    config = DaemonConfig()
    server = ShellwireServer(config)
    server._shutting_down = True

    mock_ws = AsyncMock()
    raw = json.dumps({"type": "execute", "id": "cmd-1", "command": "echo hi"})

    await server._dispatch(raw, mock_ws, "client-1")

    # Should have sent an error with code DAEMON_STOPPING.
    sent = json.loads(mock_ws.send.call_args[0][0])
    assert sent["type"] == "error"
    assert sent["code"] == "DAEMON_STOPPING"


@pytest.mark.asyncio
async def test_graceful_shutdown_sends_daemon_stopping():
    """_graceful_shutdown sends daemon_stopping to the active client."""
    config = DaemonConfig()
    server = ShellwireServer(config)

    mock_ws = AsyncMock()
    server._client_manager.active_websocket = mock_ws

    await server._graceful_shutdown()

    # Verify daemon_stopping was sent.
    sent_calls = [json.loads(c[0][0]) for c in mock_ws.send.call_args_list]
    stopping_msgs = [m for m in sent_calls if m.get("type") == "daemon_stopping"]
    assert len(stopping_msgs) == 1

    # Verify connection was closed with 1001.
    mock_ws.close.assert_called_once_with(1001, "Server shutting down")


@pytest.mark.asyncio
async def test_graceful_shutdown_idempotent():
    """Second call to _graceful_shutdown is a no-op."""
    config = DaemonConfig()
    server = ShellwireServer(config)

    mock_ws = AsyncMock()
    server._client_manager.active_websocket = mock_ws

    await server._graceful_shutdown()
    assert server._shutting_down is True

    # Reset mock to verify second call doesn't send anything.
    mock_ws.reset_mock()

    await server._graceful_shutdown()

    # Should not have sent anything on the second call.
    mock_ws.send.assert_not_called()
    mock_ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_graceful_shutdown_no_client():
    """_graceful_shutdown works when no client is connected."""
    config = DaemonConfig()
    server = ShellwireServer(config)

    # No active websocket.
    assert server._client_manager.active_websocket is None

    await server._graceful_shutdown()

    assert server._shutting_down is True


@pytest.mark.asyncio
async def test_graceful_shutdown_waits_for_commands():
    """_graceful_shutdown waits for active commands during grace period."""
    config = DaemonConfig(shutdown_grace_period=0.5)
    server = ShellwireServer(config)

    # Simulate an active command that finishes quickly.
    mock_proc = MagicMock()
    server._executor._active["cmd-1"] = mock_proc

    async def finish_soon():
        await asyncio.sleep(0.2)
        server._executor._active.pop("cmd-1", None)

    asyncio.ensure_future(finish_soon())

    await server._graceful_shutdown()

    # Command should have finished before grace period expired.
    assert "cmd-1" not in server._executor._active


@pytest.mark.asyncio
async def test_graceful_shutdown_kills_sessions():
    """_graceful_shutdown calls kill_all on sessions."""
    config = DaemonConfig(shutdown_grace_period=0.1)
    server = ShellwireServer(config)

    with patch.object(
        server._session_manager, "kill_all", new_callable=AsyncMock
    ) as mock_kill:
        await server._graceful_shutdown()
        mock_kill.assert_called_once()
