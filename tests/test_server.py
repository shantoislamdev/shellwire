import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from shellwire.config import DaemonConfig
from shellwire.server import KothaServer


@pytest.mark.asyncio
async def test_health_check_endpoint():
    config = DaemonConfig()
    server = KothaServer(config)
    
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
    server = KothaServer(config)
    
    mock_websocket = AsyncMock()
    
    with patch("shellwire.server.validate_token", return_value=False):
        # Simulate websocket yielding an invalid auth message
        mock_websocket.recv.return_value = '{"type": "auth", "token": "bad", "client_id": "test"}'
        
        await server.handler(mock_websocket)
        
        mock_websocket.close.assert_called_with(4005, "Invalid token")
