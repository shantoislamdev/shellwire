"""Client identity tracking and single-connection enforcement.

The daemon allows only **one active WebSocket** at a time.  When the same
``client_id`` reconnects, the stale socket is closed and the new one takes
over.  Revoked clients are persisted to disk so they stay rejected across
daemon restarts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CLIENTS_FILE = Path(os.path.expanduser("~/.shellwire/clients.json"))


@dataclass
class ClientRecord:
    """Metadata for a known client."""

    client_id: str
    first_seen: float  # time.time()
    last_seen: float
    is_revoked: bool = False


class ClientManager:
    """Manages client authentication, reconnection, and revocation.

    Only one active WebSocket connection is allowed at any given time.
    The same ``client_id`` may reconnect freely (the previous socket is
    closed), but a *different* ``client_id`` while one is connected will
    be rejected.

    Revoked client IDs are persisted so they survive daemon restarts.
    """

    def __init__(self) -> None:
        self.clients: Dict[str, ClientRecord] = {}
        self.active_websocket: Any = None  # websockets.WebSocketServerProtocol
        self.active_client_id: Optional[str] = None
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def authenticate(
        self,
        client_id: str,
        websocket: Any,
    ) -> Tuple[bool, str]:
        """Authenticate an incoming client connection.

        Args:
            client_id: The self-reported client identifier.
            websocket: The new WebSocket connection.

        Returns:
            A ``(accepted, reason)`` tuple.
        """
        # Reject revoked clients.
        record = self.clients.get(client_id)
        if record is not None and record.is_revoked:
            logger.warning("Rejected revoked client: %s", client_id)
            return False, "Client has been revoked"

        now = time.time()

        # Same client reconnecting → close stale socket, accept.
        if self.active_client_id == client_id and self.active_websocket is not None:
            logger.info(
                "Client %s reconnecting – closing stale socket", client_id
            )
            try:
                await self.active_websocket.close(
                    1000, "Replaced by new connection"
                )
            except Exception:
                logger.debug("Stale socket already closed")
            self.active_websocket = None

        # Different client while one is already connected → reject.
        if (
            self.active_client_id is not None
            and self.active_client_id != client_id
            and self.active_websocket is not None
        ):
            logger.warning(
                "Rejected client %s – slot occupied by %s",
                client_id,
                self.active_client_id,
            )
            return False, (
                f"Connection slot occupied by client '{self.active_client_id}'"
            )

        # Accept – register or update.
        if record is None:
            record = ClientRecord(
                client_id=client_id,
                first_seen=now,
                last_seen=now,
            )
            self.clients[client_id] = record
            logger.info("New client registered: %s", client_id)
        else:
            record.last_seen = now

        self.active_websocket = websocket
        self.active_client_id = client_id
        return True, "Authenticated"

    async def revoke(self, client_id: str) -> bool:
        """Revoke a client, force-closing its connection if active.

        Args:
            client_id: The client to revoke.

        Returns:
            ``True`` if the client existed and was revoked.
        """
        record = self.clients.get(client_id)
        if record is None:
            # Create a record just to persist the revocation.
            now = time.time()
            record = ClientRecord(
                client_id=client_id,
                first_seen=now,
                last_seen=now,
                is_revoked=True,
            )
            self.clients[client_id] = record
        else:
            record.is_revoked = True

        # Force-close if this is the active client.
        if self.active_client_id == client_id and self.active_websocket is not None:
            logger.info("Force-closing revoked client: %s", client_id)
            try:
                await self.active_websocket.close(1008, "Client revoked")
            except Exception:
                pass
            self.active_websocket = None
            self.active_client_id = None

        self._persist()
        logger.info("Client revoked: %s", client_id)
        return True

    def list_clients(self) -> List[Dict[str, Any]]:
        """Return metadata for all known clients.

        Returns:
            A list of dicts with client details.
        """
        result: List[Dict[str, Any]] = []
        for cid, rec in self.clients.items():
            result.append(
                {
                    "client_id": rec.client_id,
                    "first_seen": rec.first_seen,
                    "last_seen": rec.last_seen,
                    "is_revoked": rec.is_revoked,
                    "is_connected": (
                        self.active_client_id == cid
                        and self.active_websocket is not None
                    ),
                }
            )
        return result

    def on_disconnect(self, client_id: str) -> None:
        """Mark a client as disconnected.

        Called when the WebSocket connection closes (for any reason).

        Args:
            client_id: The disconnecting client.
        """
        if self.active_client_id == client_id:
            self.active_websocket = None
            self.active_client_id = None
            logger.info("Client disconnected: %s", client_id)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Save revoked client IDs to disk."""
        data: Dict[str, Any] = {}
        for cid, rec in self.clients.items():
            if rec.is_revoked:
                data[cid] = {
                    "first_seen": rec.first_seen,
                    "last_seen": rec.last_seen,
                    "is_revoked": True,
                }

        try:
            _CLIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: temp file + rename to prevent corruption
            # on crash.  Matches the pattern from auth.py token persistence.
            tmp_path = _CLIENTS_FILE.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            tmp_path.replace(_CLIENTS_FILE)
        except OSError as exc:
            logger.error("Failed to persist client data: %s", exc)

    def _load(self) -> None:
        """Load revoked clients from disk."""
        try:
            text = _CLIENTS_FILE.read_text(encoding="utf-8")
            data = json.loads(text)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        except OSError as exc:
            logger.error("Failed to load client data: %s", exc)
            return

        for cid, info in data.items():
            if isinstance(info, dict) and info.get("is_revoked"):
                self.clients[cid] = ClientRecord(
                    client_id=cid,
                    first_seen=info.get("first_seen", 0.0),
                    last_seen=info.get("last_seen", 0.0),
                    is_revoked=True,
                )
        logger.debug("Loaded %d revoked clients from disk", len(self.clients))
