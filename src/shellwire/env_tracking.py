# This file contains modified third-party code licensed under the MIT License. See NOTICE for details.
"""Environment tracking for persistent shell sessions.

Provides CWD resolution, session-level environment snapshots (``export -p``),
and a helper for piping stdin to a subprocess.
"""

from __future__ import annotations

import os
import shlex
import tempfile
import threading
from pathlib import Path
from typing import IO, Any


# ---------------------------------------------------------------------------
# CWD resolution
# ---------------------------------------------------------------------------

def resolve_safe_cwd(cwd: str) -> str:
    """Validate *cwd* and return the nearest existing ancestor.

    Walks the directory tree upward until an existing directory is found.
    If no ancestor exists (unlikely), falls back to
    :func:`tempfile.gettempdir`.

    Args:
        cwd: The desired working directory path.

    Returns:
        An absolute path to an existing directory.
    """
    target = Path(cwd).resolve()

    if target.is_dir():
        return str(target)

    # Walk upward to find the nearest existing ancestor.
    for parent in target.parents:
        if parent.is_dir():
            return str(parent)

    # Absolute fallback.
    return tempfile.gettempdir()


# ---------------------------------------------------------------------------
# Session snapshot
# ---------------------------------------------------------------------------

class SessionSnapshot:
    """Tracks environment variables and CWD across shell invocations.

    Each command is wrapped in a bash script that:

    1. Sources the previous session's ``export -p`` snapshot.
    2. ``cd``s to the tracked working directory.
    3. Runs the user's command.
    4. Saves the resulting environment and CWD for the next command.

    CWD is communicated back to the host process via sentinel markers
    embedded in stdout, which :meth:`update_cwd` strips before
    returning the cleaned output.
    """

    def __init__(self, session_id: str, initial_cwd: str = "") -> None:
        self._session_id = session_id
        self._cwd = resolve_safe_cwd(initial_cwd) if initial_cwd else os.getcwd()

        # Temp files for env snapshot and CWD tracking.
        self._snapshot_file = tempfile.NamedTemporaryFile(
            prefix=f"shellwire_env_{session_id}_",
            suffix=".sh",
            delete=False,
            mode="w",
        )
        self._snapshot_file.close()

        self._cwd_file = tempfile.NamedTemporaryFile(
            prefix=f"shellwire_cwd_{session_id}_",
            suffix=".txt",
            delete=False,
            mode="w",
        )
        self._cwd_file.close()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def cwd(self) -> str:
        """Return the current tracked working directory."""
        return self._cwd

    # ------------------------------------------------------------------
    # Command wrapping
    # ------------------------------------------------------------------

    def wrap_command(self, command: str, cwd: str = "") -> str:
        """Wrap *command* in a bash script that manages session state.

        Args:
            command: The user's shell command to execute.
            cwd: Override working directory.  Defaults to the tracked
                CWD from the previous invocation.

        Returns:
            A bash script string ready for execution.
        """
        effective_cwd = resolve_safe_cwd(cwd) if cwd else self._cwd
        escaped_command = command.replace("'", "'\\''")
        snapshot_path = self._snapshot_file.name
        cwd_file_path = self._cwd_file.name
        sid = self._session_id

        marker = f"__SHELLWIRE_CWD_{sid}__"

        script = (
            f"source {shlex.quote(snapshot_path)} >/dev/null 2>&1 || true\n"
            f"builtin cd -- {shlex.quote(effective_cwd)} || exit 126\n"
            f"eval '{escaped_command}'\n"
            f"__shellwire_ec=$?\n"
            f"export -p > {shlex.quote(snapshot_path)} 2>/dev/null || true\n"
            f"pwd -P > {shlex.quote(cwd_file_path)} 2>/dev/null || true\n"
            f"printf '\\n{marker}%s{marker}\\n' \"$(pwd -P)\"\n"
            f"exit $__shellwire_ec\n"
        )
        return script

    # ------------------------------------------------------------------
    # CWD extraction
    # ------------------------------------------------------------------

    def update_cwd(self, output: str) -> str:
        """Extract CWD from the sentinel marker and strip it from output.

        If the marker is found, the tracked CWD is updated and the
        marker lines are removed from the output.  If no marker is
        found, the output is returned unchanged.

        Args:
            output: Raw command output (stdout) that may contain the
                CWD sentinel marker.

        Returns:
            Cleaned output with the marker stripped.
        """
        marker = f"__SHELLWIRE_CWD_{self._session_id}__"

        start = output.find(marker)
        if start == -1:
            return output

        # Find the CWD value between the two markers.
        value_start = start + len(marker)
        end = output.find(marker, value_start)
        if end == -1:
            return output

        new_cwd = output[value_start:end].strip()
        if new_cwd and os.path.isdir(new_cwd):
            self._cwd = new_cwd

        # Strip the entire marker line (including surrounding newlines).
        line_start = output.rfind("\n", 0, start)
        line_start = line_start if line_start != -1 else start

        line_end = output.find("\n", end + len(marker))
        line_end = line_end + 1 if line_end != -1 else end + len(marker)

        cleaned = output[:line_start] + output[line_end:]
        return cleaned

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove temporary files created for this session."""
        for path in (self._snapshot_file.name, self._cwd_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Stdin piping
# ---------------------------------------------------------------------------

def pipe_stdin(proc: Any, data: str) -> None:
    """Pipe *data* to *proc*'s stdin on a daemon thread.

    Writes the data and closes stdin so the child sees EOF.  Silently
    ignores ``BrokenPipeError`` and ``OSError``.

    Args:
        proc: A subprocess-like object with a ``.stdin`` attribute
            that supports ``.write()`` and ``.close()``.
        data: String data to pipe to the process.
    """

    def _write() -> None:
        try:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            target: IO[bytes] = getattr(proc.stdin, "buffer", proc.stdin)
            target.write(raw)
            target.close()
        except (BrokenPipeError, OSError):
            pass

    threading.Thread(target=_write, daemon=True).start()
