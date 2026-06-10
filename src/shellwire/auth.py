"""Stable token management and PID file helpers.

The auth token is generated **once** on first daemon start and persists across
restarts. Only ``shellwire token rotate`` replaces it. This provides stable
authentication across daemon restarts.

All files are stored under ``~/.shellwire/``.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import signal
import time
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
    """Write the daemon PID to the PID file as a JSON record.

    The record includes the process start time from /proc/self/stat
    (field 22, 0-indexed as 21) for PID-reuse detection. On Termux/Android,
    /proc/self/stat is always readable even with hidepid=2.
    """
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    start_time = None
    try:
        with open("/proc/self/stat", encoding="utf-8") as f:
            fields = f.read().split()
            start_time = int(fields[21])  # field 22 (0-indexed)
    except (OSError, IndexError, ValueError):
        start_time = int(time.time())  # fallback: wall clock
    record = json.dumps({"pid": pid, "start_time": start_time})
    tmp_path = PID_FILE.with_suffix(".tmp")
    tmp_path.write_text(record, encoding="utf-8")
    tmp_path.replace(PID_FILE)
    logger.debug("Wrote PID %d (start_time=%s) to %s", pid, start_time, PID_FILE)


def read_pid() -> Optional[int]:
    """Read the stored daemon PID."""
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
        # Try JSON format first (new)
        try:
            record = json.loads(text)
            return int(record["pid"])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        # Fallback: legacy plain integer
        return int(text)
    except (FileNotFoundError, ValueError):
        return None
    except OSError as exc:
        logger.error("Failed to read PID file: %s", exc)
        return None


def _read_pid_record() -> Optional[dict]:
    """Read the full PID record (pid + start_time), or None."""
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
        try:
            record = json.loads(text)
            if "pid" in record:
                return record
        except (json.JSONDecodeError, TypeError):
            pass
        # Legacy plain PID — no start_time available
        pid = int(text)
        return {"pid": pid, "start_time": None}
    except (FileNotFoundError, ValueError):
        return None
    except OSError as exc:
        logger.error("Failed to read PID record: %s", exc)
        return None


def is_running() -> bool:
    """Check whether the daemon is currently running.

    Uses signal 0 for liveness, plus a start-time comparison from
    /proc/<pid>/stat to detect PID reuse after crashes. On Termux,
    /proc/<pid>/stat is readable for same-UID processes even with hidepid=2.
    """
    record = _read_pid_record()
    if record is None:
        return False
    pid = record["pid"]
    saved_start_time = record.get("start_time")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        remove_pid()
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # PID-reuse guard: compare start times
    if saved_start_time is not None:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                fields = f.read().split()
                current_start_time = int(fields[21])
                if current_start_time != saved_start_time:
                    logger.info("PID %d was reused (start_time mismatch), cleaning up", pid)
                    remove_pid()
                    return False
        except (OSError, IndexError, ValueError):
            pass  # Can't verify — trust signal-0 result
    return True


def remove_pid() -> None:
    """Remove the PID file."""
    try:
        PID_FILE.unlink()
        logger.debug("Removed PID file %s", PID_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.error("Failed to remove PID file: %s", exc)
