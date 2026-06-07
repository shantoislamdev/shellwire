"""WebSocket server – the heart of shellwire.

Ties together authentication, command execution, session management,
and client tracking behind a single WebSocket endpoint with an HTTP
``/health`` hook.
"""

from __future__ import annotations

import asyncio
import http
import json
import logging
import os
import platform
import sys
import time
from typing import Any, Dict, Optional

import websockets
from websockets.http11 import Request, Response

from shellwire import __version__
from shellwire.auth import validate_token
from shellwire.client_manager import ClientManager
from shellwire.config import DaemonConfig
from shellwire.executor import CommandExecutor
from shellwire.protocol import (
    ErrorMessage,
    OutputMessage,
    PongMessage,
    ResultMessage,
    SessionEndedMessage,
    SessionStartedMessage,
    SessionsListMessage,
    StatusMessage,
    deserialize,
    serialize,
    validate_message,
)
from shellwire.session import SessionManager

logger = logging.getLogger(__name__)


class KothaServer:
    """WebSocket server that bridges shell commands from KothaCode.

    Lifecycle::

        server = KothaServer(config)
        await server.serve()          # blocks until shutdown
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._executor = CommandExecutor(config)
        self._session_manager = SessionManager(config)
        self._client_manager = ClientManager()
        self._start_time = time.time()
        self._shell = os.environ.get("SHELL", "/bin/sh")

    # ------------------------------------------------------------------
    # Health check (HTTP on same port)
    # ------------------------------------------------------------------

    async def health_check_handler(
        self,
        connection: Any,
        request: Request,
    ) -> Optional[Response]:
        """Handle HTTP requests on the WebSocket port.

        Non-WebSocket ``GET /health`` requests receive a JSON health
        response.  Everything else is passed through to the WebSocket
        handler.

        This method is wired as the ``process_request`` hook.
        """
        if request.path == "/health":
            body = json.dumps(
                {
                    "status": "ok",
                    "version": __version__,
                    "uptime_seconds": round(time.time() - self._start_time, 1),
                    "active_commands": self._executor.active_count,
                    "active_sessions": self._session_manager.active_count,
                    "python_version": platform.python_version(),
                },
                indent=2,
            ).encode("utf-8")

            return Response(
                status_code=200,
                reason_phrase="OK",
                headers=websockets.Headers(
                    [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ]
                ),
                body=body,
            )

        # Return None to let the WebSocket handshake proceed.
        return None

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def handler(self, websocket: Any) -> None:
        """Handle a single WebSocket connection lifecycle.

        1. Wait for an ``auth`` message.
        2. Validate token + client_id.
        3. Enter the command loop until disconnection.
        """
        client_id: Optional[str] = None

        try:
            client_id = await self._authenticate(websocket)
            if client_id is None:
                return

            # Send status message upon successful auth.
            await self._send(
                websocket,
                StatusMessage(
                    version=__version__,
                    uptime_seconds=round(
                        time.time() - self._start_time, 1
                    ),
                    active_commands=self._executor.active_count,
                    active_sessions=self._session_manager.active_count,
                    python_version=platform.python_version(),
                    shell=self._shell,
                    client_id=client_id,
                ),
            )

            # Command loop.
            async for raw in websocket:
                await self._dispatch(raw, websocket, client_id)

        except websockets.exceptions.ConnectionClosed:
            logger.info("Connection closed: %s", client_id or "unknown")
        except Exception:
            logger.error("Unhandled error in handler", exc_info=True)
        finally:
            if client_id is not None:
                self._client_manager.on_disconnect(client_id)

    # ------------------------------------------------------------------
    # Auth handshake
    # ------------------------------------------------------------------

    async def _authenticate(self, websocket: Any) -> Optional[str]:
        """Wait for an auth message and validate credentials.

        Returns:
            The ``client_id`` on success, or ``None`` if rejected.
        """
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            await self._send_error(websocket, None, "Auth timeout", "AUTH_TIMEOUT")
            await websocket.close(4001, "Auth timeout")
            return None
        except websockets.exceptions.ConnectionClosed:
            return None

        try:
            data = deserialize(raw)
        except ValueError as exc:
            await self._send_error(websocket, None, str(exc), "INVALID_JSON")
            await websocket.close(4002, "Invalid JSON")
            return None

        if data.get("type") != "auth":
            await self._send_error(
                websocket, None, "First message must be auth", "AUTH_REQUIRED"
            )
            await websocket.close(4003, "Auth required")
            return None

        token = data.get("token", "")
        client_id = data.get("client_id", "")

        if not client_id:
            await self._send_error(
                websocket, None, "client_id is required", "MISSING_CLIENT_ID"
            )
            await websocket.close(4004, "Missing client_id")
            return None

        if not validate_token(token):
            await self._send_error(
                websocket, None, "Invalid token", "INVALID_TOKEN"
            )
            await websocket.close(4005, "Invalid token")
            return None

        accepted, reason = await self._client_manager.authenticate(
            client_id, websocket
        )
        if not accepted:
            await self._send_error(
                websocket, None, reason, "CLIENT_REJECTED"
            )
            await websocket.close(4006, reason)
            return None

        logger.info("Client authenticated: %s", client_id)
        return client_id

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self, raw: str, websocket: Any, client_id: str
    ) -> None:
        """Parse and route an incoming message."""
        try:
            data = deserialize(raw)
        except ValueError as exc:
            await self._send_error(websocket, None, str(exc), "INVALID_JSON")
            return

        if not validate_message(data):
            await self._send_error(
                websocket,
                data.get("id"),
                "Invalid message format",
                "INVALID_MESSAGE",
            )
            return

        msg_type = data["type"]

        handlers = {
            "execute": self._handle_execute,
            "start_session": self._handle_start_session,
            "send_input": self._handle_send_input,
            "kill_session": self._handle_kill_session,
            "list_sessions": self._handle_list_sessions,
            "ping": self._handle_ping,
        }

        handler = handlers.get(msg_type)
        if handler is None:
            await self._send_error(
                websocket,
                data.get("id"),
                f"Unknown message type: {msg_type}",
                "UNKNOWN_TYPE",
            )
            return

        try:
            await handler(data, websocket)
        except Exception as exc:
            logger.error("Handler error for %s", msg_type, exc_info=True)
            await self._send_error(
                websocket,
                data.get("id"),
                f"Internal error: {exc}",
                "INTERNAL_ERROR",
            )

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _handle_execute(
        self, data: Dict[str, Any], websocket: Any
    ) -> None:
        """Handle a one-shot ``execute`` message."""
        command_id = data["id"]
        command = data["command"]
        timeout = data.get("timeout", self._config.default_timeout)

        async def on_output(
            cmd_id: str, text: str, stream: str
        ) -> None:
            await self._send(
                websocket,
                OutputMessage(id=cmd_id, data=text, stream=stream),
            )

        # Run in a task so the command loop can continue receiving messages.
        asyncio.ensure_future(
            self._execute_and_report(
                command_id, command, timeout, on_output, websocket
            )
        )

    async def _execute_and_report(
        self,
        command_id: str,
        command: str,
        timeout: int,
        on_output: Any,
        websocket: Any,
    ) -> None:
        """Execute a command and send the result."""
        try:
            result = await self._executor.execute(
                command_id, command, timeout=timeout, on_output=on_output
            )
            await self._send(
                websocket,
                ResultMessage(
                    id=command_id,
                    exit_code=result["exit_code"],
                    duration_ms=result["duration_ms"],
                ),
            )
        except Exception as exc:
            await self._send_error(
                websocket, command_id, str(exc), "EXECUTION_ERROR"
            )

    async def _handle_start_session(
        self, data: Dict[str, Any], websocket: Any
    ) -> None:
        """Handle a ``start_session`` message."""
        session_id = data["id"]
        command = data["command"]

        async def on_output(
            sid: str, text: str, stream: str
        ) -> None:
            # Check for the sentinel end-of-session signal.
            if stream.startswith("__ended__:"):
                parts = stream.split(":")
                exit_code_str = parts[1] if len(parts) > 1 else "-1"
                duration_str = parts[2] if len(parts) > 2 else "0"
                try:
                    exit_code = int(exit_code_str) if exit_code_str != "None" else None
                except ValueError:
                    exit_code = None
                try:
                    duration_ms = float(duration_str)
                except ValueError:
                    duration_ms = 0.0
                await self._send(
                    websocket,
                    SessionEndedMessage(
                        id=sid,
                        exit_code=exit_code,
                        duration_ms=duration_ms,
                    ),
                )
                return

            await self._send(
                websocket,
                OutputMessage(id=sid, data=text, stream=stream),
            )

        try:
            session = await self._session_manager.start_session(
                session_id, command, on_output=on_output
            )
            await self._send(
                websocket,
                SessionStartedMessage(id=session_id, pid=session.process.pid),
            )
        except ValueError as exc:
            await self._send_error(
                websocket, session_id, str(exc), "SESSION_ERROR"
            )

    async def _handle_send_input(
        self, data: Dict[str, Any], websocket: Any
    ) -> None:
        """Handle a ``send_input`` message."""
        session_id = data["id"]
        input_data = data["data"]

        try:
            await self._session_manager.send_input(session_id, input_data)
        except (KeyError, RuntimeError) as exc:
            await self._send_error(
                websocket, session_id, str(exc), "SESSION_ERROR"
            )

    async def _handle_kill_session(
        self, data: Dict[str, Any], websocket: Any
    ) -> None:
        """Handle a ``kill_session`` message."""
        session_id = data["id"]

        try:
            result = await self._session_manager.kill_session(session_id)
            await self._send(
                websocket,
                SessionEndedMessage(
                    id=session_id,
                    exit_code=result["exit_code"],
                    duration_ms=result["duration_ms"],
                ),
            )
        except KeyError as exc:
            await self._send_error(
                websocket, session_id, str(exc), "SESSION_NOT_FOUND"
            )

    async def _handle_list_sessions(
        self, data: Dict[str, Any], websocket: Any
    ) -> None:
        """Handle a ``list_sessions`` message."""
        sessions = self._session_manager.list_sessions()
        await self._send(
            websocket, SessionsListMessage(sessions=sessions)
        )

    async def _handle_ping(
        self, data: Dict[str, Any], websocket: Any
    ) -> None:
        """Handle a ``ping`` message."""
        await self._send(websocket, PongMessage())

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------

    async def _send(self, websocket: Any, msg: Any) -> None:
        """Serialize and send a protocol message."""
        try:
            await websocket.send(serialize(msg))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send – connection already closed")

    async def _send_error(
        self,
        websocket: Any,
        msg_id: Optional[str],
        message: str,
        code: str,
    ) -> None:
        """Send an error message."""
        await self._send(
            websocket,
            ErrorMessage(id=msg_id, message=message, code=code),
        )

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def serve(self) -> None:
        """Start the WebSocket server and block until shutdown.

        Installs signal handlers for SIGTERM/SIGINT for graceful shutdown.
        """
        loop = asyncio.get_event_loop()

        stop = asyncio.Future()  # type: asyncio.Future[None]

        # Install signal handlers (POSIX only).
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set_result, None)
            except NotImplementedError:
                # Windows – signals not supported in the same way.
                pass

        logger.info(
            "Starting shellwire v%s on %s:%d",
            __version__,
            self._config.host,
            self._config.port,
        )

        async with websockets.serve(
            self.handler,
            self._config.host,
            self._config.port,
            process_request=self.health_check_handler,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
            max_size=2**20,  # 1 MB max message
        ):
            logger.info(
                "Server listening on ws://%s:%d",
                self._config.host,
                self._config.port,
            )
            try:
                await stop
            except asyncio.CancelledError:
                pass

        # Graceful shutdown.
        logger.info("Shutting down...")
        await self._session_manager.kill_all()
        logger.info("Server stopped")
