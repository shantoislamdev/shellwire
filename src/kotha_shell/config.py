"""Daemon configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DaemonConfig:
    """Configuration for the kotha-shell WebSocket daemon.

    All values have sane defaults for a Termux environment.
    Paths are expanded at access time via properties.
    """

    host: str = "127.0.0.1"
    port: int = 7842
    max_concurrent_commands: int = 4
    default_timeout: int = 120
    max_output_size: int = 512_000  # 512 KB
    max_sessions: int = 8
    log_level: str = "INFO"
    log_file: str = "~/.kotha-shell/daemon.log"

    @property
    def resolved_log_file(self) -> Path:
        """Return the log file path with ``~`` expanded."""
        return Path(os.path.expanduser(self.log_file))

    @property
    def data_dir(self) -> Path:
        """Return the data directory (``~/.kotha-shell``)."""
        return Path(os.path.expanduser("~/.kotha-shell"))

    def ensure_dirs(self) -> None:
        """Create required directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
