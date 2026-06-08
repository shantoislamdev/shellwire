# This file contains modified third-party code licensed under the MIT License. See NOTICE for details.
"""POSIX-only PTY bridge wrapping ``ptyprocess.PtyProcess``.

This module is only
functional on POSIX systems (Linux, macOS).  On Windows or when
``ptyprocess`` is not installed, :meth:`PtyBridge.is_available` returns
``False`` and :meth:`PtyBridge.spawn` raises :exc:`PtyUnavailableError`.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# POSIX-only imports — gated behind platform check.
# ---------------------------------------------------------------------------
_POSIX = os.name == "posix"

if _POSIX:
    import fcntl       # noqa: F401
    import select
    import signal
    import struct
    import termios     # noqa: F401
    import time

try:
    import ptyprocess  # type: ignore[import-untyped]
    _HAS_PTYPROCESS = True
except ImportError:
    ptyprocess = None  # type: ignore[assignment]
    _HAS_PTYPROCESS = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_DIMENSION = 1
_MAX_COLS = 2000
_MAX_ROWS = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp_dimension(value: int, maximum: int) -> int:
    """Clamp a terminal dimension to ``[_MIN_DIMENSION, maximum]``."""
    return max(_MIN_DIMENSION, min(value, maximum))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PtyUnavailableError(RuntimeError):
    """Raised when PTY support is not available on this platform."""


# ---------------------------------------------------------------------------
# PtyBridge
# ---------------------------------------------------------------------------

class PtyBridge:
    """Thin wrapper around ``ptyprocess.PtyProcess``.

    Provides a consistent interface for spawning commands in a PTY,
    reading/writing data, resizing the terminal, and tearing down the
    process with signal escalation.

    Usage::

        with PtyBridge.spawn(["bash"], cwd="/tmp") as pty:
            pty.write(b"echo hello\\n")
            data = pty.read()
    """

    def __init__(self, proc: ptyprocess.PtyProcess) -> None:
        self._proc = proc
        self._fd: int = proc.fd
        self._closed = False

    # ------------------------------------------------------------------
    # Class-level availability
    # ------------------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        """Return ``True`` if PTY support is available on this platform."""
        return _POSIX and _HAS_PTYPROCESS

    # ------------------------------------------------------------------
    # Spawning
    # ------------------------------------------------------------------

    @classmethod
    def spawn(
        cls,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> PtyBridge:
        """Spawn a new PTY process.

        Args:
            argv: Command and arguments to execute.
            cwd: Working directory for the child process.
            env: Environment variables.  ``TERM`` is forced to
                ``xterm-256color`` if not already set.
            cols: Initial terminal width.
            rows: Initial terminal height.

        Returns:
            A new :class:`PtyBridge` instance.

        Raises:
            PtyUnavailableError: If PTY support is not available.
        """
        if not cls.is_available():
            raise PtyUnavailableError(
                "PTY support requires a POSIX system with ptyprocess installed"
            )

        cols = _clamp_dimension(cols, _MAX_COLS)
        rows = _clamp_dimension(rows, _MAX_ROWS)

        spawn_env = dict(env) if env else dict(os.environ)
        spawn_env.setdefault("TERM", "xterm-256color")

        proc = ptyprocess.PtyProcess.spawn(
            argv,
            cwd=cwd,
            env=spawn_env,
            dimensions=(rows, cols),
        )
        return cls(proc)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pid(self) -> int:
        """Return the PID of the child process."""
        return self._proc.pid

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def is_alive(self) -> bool:
        """Return ``True`` if the child process is still running."""
        return self._proc.isalive()

    def read(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read available data from the PTY.

        Args:
            timeout: Maximum seconds to wait for data.

        Returns:
            Raw bytes if data is available, ``b""`` if the timeout
            expired with no data, or ``None`` if the PTY has been
            closed / reached EOF.
        """
        if self._closed:
            return None

        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
        except (ValueError, OSError):
            return None

        if not ready:
            return b""

        try:
            data = os.read(self._fd, 4096)
        except OSError:
            return None

        if not data:
            return None

        return data

    def write(self, data: bytes) -> None:
        """Write *data* to the PTY, handling short writes.

        Uses a :class:`memoryview` loop to ensure all bytes are
        delivered even if the kernel accepts fewer than requested.
        """
        if self._closed:
            return

        view = memoryview(data)
        while view:
            try:
                written = os.write(self._fd, view)
            except OSError:
                return
            view = view[written:]

    # ------------------------------------------------------------------
    # Resize
    # ------------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY to *cols* × *rows*.

        Uses ``TIOCSWINSZ`` ioctl with clamped dimensions.
        """
        if self._closed:
            return

        cols = _clamp_dimension(cols, _MAX_COLS)
        rows = _clamp_dimension(rows, _MAX_ROWS)

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
        except (OSError, AttributeError):
            pass

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the PTY and terminate the child process.

        Uses SIGHUP → SIGTERM → SIGKILL escalation with 500ms grace
        periods between each signal.
        """
        if self._closed:
            return
        self._closed = True

        pid = self._proc.pid

        # Signal escalation: SIGHUP → SIGTERM → SIGKILL.
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            if not self._proc.isalive():
                break
            try:
                os.kill(pid, sig)
            except OSError:
                break
            # 500ms grace period.
            time.sleep(0.5)

        try:
            self._proc.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> PtyBridge:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
