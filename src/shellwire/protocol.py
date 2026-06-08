"""Wire protocol: message types, serialization, and validation.

Every message is a JSON object with a ``type`` field that determines its
schema.  Client→Server and Server→Client message shapes are defined as
dataclasses so the rest of the codebase never touches raw dicts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client → Server messages
# ---------------------------------------------------------------------------


@dataclass
class AuthMessage:
    """Initial handshake sent by the client after connecting."""

    token: str
    client_id: str
    type: str = "auth"


@dataclass
class ExecuteMessage:
    """One-shot command execution request."""

    id: str
    command: str
    timeout: int = 120
    cwd: str = ""  # working directory (empty = inherit daemon cwd)
    env: Optional[Dict[str, str]] = None  # extra environment variables
    stdin_data: str = ""  # data to pipe to stdin before execution
    type: str = "execute"


@dataclass
class StartSessionMessage:
    """Start a long-running interactive session."""

    id: str
    command: str
    use_pty: bool = False  # spawn behind a pseudo-terminal
    cols: int = 80  # initial terminal width (PTY mode only)
    rows: int = 24  # initial terminal height (PTY mode only)
    type: str = "start_session"


@dataclass
class SendInputMessage:
    """Send stdin data to a running session."""

    id: str
    data: str
    close_stdin: bool = False  # close stdin after writing (send EOF)
    type: str = "send_input"


@dataclass
class ResizeMessage:
    """Resize the PTY terminal of a running session."""

    id: str
    cols: int
    rows: int
    type: str = "resize"


@dataclass
class KillSessionMessage:
    """Terminate a running session."""

    id: str
    type: str = "kill_session"


@dataclass
class ListSessionsMessage:
    """Request a list of active sessions."""

    type: str = "list_sessions"


@dataclass
class PingMessage:
    """Keep-alive ping."""

    type: str = "ping"


# ---------------------------------------------------------------------------
# Server → Client messages
# ---------------------------------------------------------------------------


@dataclass
class StatusMessage:
    """Server status / auth-success response."""

    version: str
    uptime_seconds: float
    active_commands: int
    active_sessions: int
    python_version: str
    shell: str
    client_id: str
    type: str = "status"


@dataclass
class OutputMessage:
    """Incremental output from a command or session."""

    id: str
    data: str
    stream: str  # "stdout" | "stderr"
    type: str = "output"


@dataclass
class ResultMessage:
    """Final result of a one-shot command."""

    id: str
    exit_code: int
    duration_ms: float
    type: str = "result"


@dataclass
class ErrorMessage:
    """Error response."""

    id: Optional[str]
    message: str
    code: str
    type: str = "error"


@dataclass
class SessionStartedMessage:
    """Confirmation that a session has started."""

    id: str
    pid: int
    type: str = "session_started"


@dataclass
class SessionEndedMessage:
    """Notification that a session has ended."""

    id: str
    exit_code: Optional[int]
    duration_ms: float
    type: str = "session_ended"


@dataclass
class SessionsListMessage:
    """Response to a list_sessions request."""

    sessions: List[Dict[str, Any]]
    type: str = "sessions_list"


@dataclass
class PongMessage:
    """Keep-alive pong response."""

    type: str = "pong"


@dataclass
class DaemonStoppingMessage:
    """Notification that the daemon is shutting down."""

    type: str = "daemon_stopping"


@dataclass
class CommandQueuedMessage:
    """Notification that a command has been queued (not yet running)."""

    id: str
    position: int
    type: str = "command_queued"


# ---------------------------------------------------------------------------
# Required fields per message type
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: Dict[str, List[str]] = {
    "auth": ["token", "client_id"],
    "execute": ["id", "command"],
    "start_session": ["id", "command"],
    "send_input": ["id", "data"],
    "kill_session": ["id"],
    "resize": ["id", "cols", "rows"],
    "list_sessions": [],
    "ping": [],
    "daemon_stopping": [],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def serialize(msg: Any) -> str:
    """Serialize a dataclass message to a JSON string.

    Args:
        msg: Any protocol dataclass instance.

    Returns:
        A compact JSON string ready to send over the wire.
    """
    return json.dumps(asdict(msg), separators=(",", ":"))


def deserialize(text: str) -> Dict[str, Any]:
    """Deserialize a JSON string into a plain dict.

    Args:
        text: Raw JSON text received from the wire.

    Returns:
        Parsed dict.

    Raises:
        ValueError: If *text* is not valid JSON.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Message must be a JSON object")

    return data


def validate_message(data: Dict[str, Any]) -> bool:
    """Validate that *data* has all required fields for its ``type``.

    Args:
        data: A parsed message dict.

    Returns:
        ``True`` if the message is structurally valid, ``False`` otherwise.
    """
    msg_type = data.get("type")
    if msg_type is None:
        logger.warning("Message missing 'type' field")
        return False

    required = _REQUIRED_FIELDS.get(msg_type)
    if required is None:
        logger.warning("Unknown message type: %s", msg_type)
        return False

    for field_name in required:
        if field_name not in data:
            logger.warning(
                "Message type '%s' missing required field '%s'",
                msg_type,
                field_name,
            )
            return False

    return True
