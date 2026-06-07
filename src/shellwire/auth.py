"""Stable token management and PID file helpers.

The auth token is generated **once** on first daemon start and persists across
restarts.  Only ``shellwire token rotate`` replaces it.  This avoids breaking
the KothaCode app every time the host system reboots.

All files are stored under ``~/.shellwire/``.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import signal
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_DIR: Path = Path(os.path.expanduser("~/.shellwire"))
TOKEN_FILE: Path = TOKEN_DIR / "auth.token"
PID_FILE: Path = TOKEN_DIR / "daemon.pid"


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def _create_new_token() -> str:
    """Generate a cryptographically secure 32-byte hex token and persist it.

    The token file is created with mode ``0o600`` (owner read/write only).

    Returns:
        The newly generated token string (64 hex characters).
    """
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    token = secrets.token_hex(32)

    # Write atomically: write to tmp, then rename.
    tmp_path = TOKEN_FILE.with_suffix(".tmp")
    tmp_path.write_text(token, encoding="utf-8")

    # chmod 600 – best-effort on platforms that support it.
    try:
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        logger.debug("Could not set file permissions on %s", tmp_path)

    tmp_path.replace(TOKEN_FILE)
    logger.info("New auth token generated and saved to %s", TOKEN_FILE)
    return token


def ensure_token() -> str:
    """Return the existing token, or generate a new one on first start.

    This is the primary entry-point used by ``shellwire start``.  It guarantees
    idempotency: calling it multiple times always returns the same token
    unless ``rotate_token()`` has been called in between.

    Returns:
        The current auth token.
    """
    existing = read_token()
    if existing is not None:
        logger.debug("Using existing auth token from %s", TOKEN_FILE)
        return existing
    return _create_new_token()


def rotate_token() -> str:
    """Generate a new token, replacing the old one.

    Only called by ``shellwire token rotate``.

    Returns:
        The freshly generated token.
    """
    logger.info("Rotating auth token")
    return _create_new_token()


def read_token() -> Optional[str]:
    """Read the persisted token, if it exists.

    Returns:
        The token string, or ``None`` if the file does not exist.
    """
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return token if token else None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("Failed to read token file: %s", exc)
        return None


def validate_token(provided: str) -> bool:
    """Compare *provided* against the stored token in constant time.

    Uses :func:`hmac.compare_digest` to prevent timing side-channels.

    Args:
        provided: The token string supplied by the client.

    Returns:
        ``True`` if the token matches, ``False`` otherwise.
    """
    stored = read_token()
    if stored is None:
        logger.warning("No stored token found – rejecting authentication")
        return False
    return hmac.compare_digest(stored, provided)


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def write_pid(pid: int) -> None:
    """Write the daemon PID to the PID file.

    Args:
        pid: The process ID to record.
    """
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")
    logger.debug("Wrote PID %d to %s", pid, PID_FILE)


def read_pid() -> Optional[int]:
    """Read the stored daemon PID.

    Returns:
        The PID as an integer, or ``None`` if the file doesn't exist or
        contains invalid data.
    """
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
        return int(text)
    except (FileNotFoundError, ValueError):
        return None
    except OSError as exc:
        logger.error("Failed to read PID file: %s", exc)
        return None


def is_running() -> bool:
    """Check whether the daemon is currently running.

    Sends signal 0 to the stored PID to test liveness.

    Returns:
        ``True`` if a process with the stored PID exists.
    """
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # Stale PID file – clean it up.
        remove_pid()
        return False
    except PermissionError:
        # Process exists but we can't signal it (shouldn't happen in user-space).
        return True
    except OSError:
        return False


def remove_pid() -> None:
    """Remove the PID file."""
    try:
        PID_FILE.unlink()
        logger.debug("Removed PID file %s", PID_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.error("Failed to remove PID file: %s", exc)
