"""Daemon configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DaemonConfig:
    """Configuration for the shellwire WebSocket daemon.

    All values have sane defaults for a Unix environment.
    Paths are expanded at access time via properties.
    """

    host: str = "127.0.0.1"
    port: int = 7842
    max_concurrent_commands: int = 4
    max_queue_size: int = 16
    default_timeout: int = 120
    max_sessions: int = 8
    shutdown_grace_period: float = 5.0  # seconds to wait for commands before killing
    log_level: str = "INFO"
    log_file: str = "~/.shellwire/daemon.log"

    # Output processing
    max_output_size: int = 512_000  # 512 KB (streaming byte cap)
    max_output_chars: int = 50_000  # head/tail truncation threshold for accumulated output
    # NOTE: No strip_ansi config — ANSI stripping is client-side (see output.py)

    # Shell execution
    shell_path: str = ""  # override shell binary (default: auto-detect via $SHELL)
    rewrite_compound_background: bool = True  # fix A && B & subshell-wait trap

    # Session settings
    session_idle_timeout: int = 3600  # kill idle sessions after 1 hour (0 = disabled)
    enable_pty: bool = True  # allow PTY sessions (requires ptyprocess)

    @property
    def resolved_log_file(self) -> Path:
        """Return the log file path with ``~`` expanded."""
        return Path(os.path.expanduser(self.log_file))

    @property
    def data_dir(self) -> Path:
        """Return the data directory (``~/.shellwire``)."""
        return Path(os.path.expanduser("~/.shellwire"))

    def ensure_dirs(self) -> None:
        """Create required directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
