import pytest
from unittest.mock import AsyncMock, patch

from shellwire.client_manager import ClientManager


@pytest.mark.asyncio
async def test_authenticate():
    manager = ClientManager()
    
    ws_mock = AsyncMock()
    success, reason = await manager.authenticate("client-1", ws_mock)
    
    assert success is True
    assert "client-1" in manager.clients

@pytest.mark.asyncio
async def test_reconnection_revokes_old():
    manager = ClientManager()
    
    old_ws = AsyncMock()
    new_ws = AsyncMock()
    
    await manager.authenticate("client-1", old_ws)
    success, reason = await manager.authenticate("client-1", new_ws)
    
    assert success is True
    old_ws.close.assert_called_once()
    assert "client-1" in manager.clients

def test_on_disconnect():
    manager = ClientManager()
    manager.active_client_id = "client-1"
    manager.active_websocket = "ws"
    
    manager.on_disconnect("client-1")
    
    assert manager.active_client_id is None
    assert manager.active_websocket is None
