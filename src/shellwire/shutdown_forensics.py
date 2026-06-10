"""Shutdown forensics — capture context when the daemon receives SIGTERM/SIGINT.

Termux/Android-adapted version of the Hermes gateway shutdown forensics.

The daemon's ``shutdown_signal_handler`` runs synchronously inside the
asyncio event loop.  We can't safely block it for long, but we DO want a
durable record of who/what triggered the shutdown so that "the daemon
keeps dying" incidents can be diagnosed after the fact.

This module exposes :func:`snapshot_shutdown_context`, a fast (<10ms),
non-blocking probe that returns a structured dict the signal handler can
log immediately.

Stripped vs. Hermes upstream
----------------------------
- **Removed** ``spawn_async_diagnostic()`` — ``dmesg``, ``pstree``, and
  ``ps auxf`` require root on Android, and forking a child wastes one of
  the kernel's phantom-process-killer slots (Android 12+).
- **Removed** ``check_systemd_timing_alignment()`` and
  ``_parse_systemd_duration_to_us()`` — no systemd on Termux.
- **Removed** systemd env vars (``INVOCATION_ID``, ``JOURNAL_STREAM``,
  ``under_systemd``) and takeover/planned-stop marker reads.
- **Added** ``android_hints`` dict with Termux wake-lock detection and
  phantom-process-killer warning.

.. note::
   Parent-process ``/proc`` reads may fail with ``EACCES`` when the
   kernel is mounted with ``hidepid=2`` (common on stock Android ROMs).
   All ``/proc`` accessors silently return ``None`` in that case.
"""

from __future__ import annotations

import json
import os
import signal
import time
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Signal name lookup table — built once at import time
# ---------------------------------------------------------------------------

_SIGNAL_NAME_BY_NUM: Dict[int, str] = {}
for _name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2"):
    _val = getattr(signal, _name, None)
    if _val is not None:
        _SIGNAL_NAME_BY_NUM[int(_val)] = _name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal_name(sig: Any) -> str:
    """Return a human-readable signal name (or ``str(sig)`` as fallback)."""
    if sig is None:
        return "UNKNOWN"
    try:
        sig_int = int(sig)
    except (TypeError, ValueError):
        return str(sig)
    return _SIGNAL_NAME_BY_NUM.get(sig_int, f"signal#{sig_int}")


def _read_proc_field(pid: int, key: str) -> Optional[str]:
    """Read a single field from ``/proc/<pid>/status``.

    Returns ``None`` on any failure — including ``hidepid=2`` mounts that
    block cross-UID ``/proc`` access (common on stock Android).
    """
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def _read_proc_cmdline(pid: int) -> Optional[str]:
    """Read ``/proc/<pid>/cmdline`` as a printable string.

    Returns ``None`` on any failure — including ``hidepid=2`` restrictions.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not data:
        return None
    # cmdline uses NUL separators
    return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _proc_summary(pid: int) -> Dict[str, Any]:
    """Compact ``/proc/<pid>`` snapshot: pid, ppid, state, uid, cmdline.

    Best-effort.  Missing fields are simply omitted rather than raising.
    On Android with ``hidepid=2``, reads for other UIDs will silently
    return empty dicts.
    """
    summary: Dict[str, Any] = {"pid": pid}
    if pid <= 0:
        return summary
    name = _read_proc_field(pid, "Name")
    if name is not None:
        summary["name"] = name
    state = _read_proc_field(pid, "State")
    if state is not None:
        summary["state"] = state
    ppid = _read_proc_field(pid, "PPid")
    if ppid is not None:
        try:
            summary["ppid"] = int(ppid)
        except ValueError:
            pass
    uid = _read_proc_field(pid, "Uid")
    if uid is not None:
        # "real effective saved fs"
        summary["uid"] = uid.split()[0] if uid else uid
    cmdline = _read_proc_cmdline(pid)
    if cmdline:
        # Truncate aggressively — these can be 4KB
        summary["cmdline"] = cmdline[:300]
    return summary


# ---------------------------------------------------------------------------
# Android / Termux hints
# ---------------------------------------------------------------------------

def _collect_android_hints() -> Dict[str, Any]:
    """Gather Termux-specific context that aids shutdown diagnosis.

    Currently checks:
    * Whether the Termux wake-lock is held (``$PREFIX/bin/termux-wake-lock``
      writes to ``~/.termux/wake_lock`` or can be inferred from the
      ``TERMUX_APK_WAKE_LOCK`` env var / the PowerManager partial lock).
    * Whether ``$TERMUX_APP__PHANTOM_PROCESS_KILL`` hints at the phantom
      process killer being active (Android 12+).

    Never raises.
    """
    hints: Dict[str, Any] = {}
    try:
        # Wake-lock detection: Termux sets an env hint when wake-lock is
        # acquired via ``termux-wake-lock``.  Also check the marker file.
        prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
        home = os.environ.get("HOME", "/data/data/com.termux/files/home")

        # Method 1: environment variable (set by newer Termux versions)
        wake_lock_env = os.environ.get("TERMUX_APP__WAKE_LOCK")
        if wake_lock_env is not None:
            hints["wake_lock_env"] = wake_lock_env

        # Method 2: marker file left by termux-wake-lock helper
        wake_lock_marker = os.path.join(home, ".termux", "wake_lock")
        try:
            hints["wake_lock_file_exists"] = os.path.exists(wake_lock_marker)
        except OSError:
            pass

        # Phantom process killer hint (Android 12+ / API 31+)
        # If the system property or env signals the killer is active, record it.
        ppk_env = os.environ.get("TERMUX_APP__PHANTOM_PROCESS_KILL")
        if ppk_env is not None:
            hints["phantom_process_kill"] = ppk_env

        # Record Termux app version if available
        app_version = os.environ.get("TERMUX_VERSION")
        if app_version:
            hints["termux_version"] = app_version

    except Exception:  # noqa: BLE001 — never raise from a signal handler
        pass
    return hints


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------

def snapshot_shutdown_context(received_signal: Any = None) -> Dict[str, Any]:
    """Fast (<10ms) snapshot of who/what is asking us to shut down.

    Captures:

    * The signal number/name (so SIGINT vs SIGTERM is visible)
    * Our own PID/ppid + parent process info from ``/proc`` (Linux)
    * ``/proc/self`` limits + load average (1-min)
    * TracerPid (debugger / strace detection)
    * ``android_hints`` — Termux wake-lock state and phantom-process-killer
      detection
    * Wall-clock and monotonic timestamps for cross-correlating later phases

    Pure stdlib, never raises, never blocks on subprocesses.

    .. note::
       Parent process ``/proc`` reads may fail with ``EACCES`` on devices
       with ``hidepid=2`` (common on stock Android ROMs).  The ``parent``
       dict will simply be ``{"pid": <ppid>}`` in that case.
    """
    now = time.time()
    monotonic = time.monotonic()
    pid = os.getpid()
    ppid = os.getppid()

    ctx: Dict[str, Any] = {
        "ts": now,
        "ts_monotonic": monotonic,
        "signal": _signal_name(received_signal),
        "signal_num": int(received_signal) if received_signal is not None else None,
        "pid": pid,
        "ppid": ppid,
        "parent": _proc_summary(ppid),
        "self": _proc_summary(pid),
    }

    # Load average — high load points the finger at "something else
    # crushing the device" rather than "external killer".
    try:
        ctx["loadavg_1m"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass

    # /proc/self/status TracerPid: nonzero means a debugger / strace is
    # attached.  Useful when "phantom SIGKILL" turns out to be a manual
    # gdb session.
    try:
        tracer = _read_proc_field(pid, "TracerPid")
        if tracer is not None and tracer != "0":
            ctx["tracer_pid"] = int(tracer) if tracer.isdigit() else tracer
            ctx["tracer"] = _proc_summary(int(tracer)) if tracer.isdigit() else None
    except (TypeError, ValueError):
        pass

    # Android / Termux-specific hints
    ctx["android_hints"] = _collect_android_hints()

    return ctx


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_context_for_log(ctx: Dict[str, Any]) -> str:
    """Render a shutdown context dict as a single, scannable log line."""
    sig = ctx.get("signal", "?")
    parent = ctx.get("parent") or {}
    parent_cmd = parent.get("cmdline", "(unknown)")
    parent_name = parent.get("name") or "?"
    parent_pid = parent.get("pid") or "?"
    load = ctx.get("loadavg_1m")
    load_str = f"{load:.2f}" if isinstance(load, (int, float)) else "?"

    extras: List[str] = []
    if ctx.get("tracer_pid"):
        extras.append(f"tracer_pid={ctx['tracer_pid']}")

    # Android-specific extras
    android = ctx.get("android_hints") or {}
    if android.get("wake_lock_env"):
        extras.append(f"wake_lock={android['wake_lock_env']}")
    elif android.get("wake_lock_file_exists"):
        extras.append("wake_lock=file_present")
    if android.get("phantom_process_kill"):
        extras.append(f"ppk={android['phantom_process_kill']}")

    extras_str = (" " + " ".join(extras)) if extras else ""

    # Parent cmdline is the most useful single signal — log it prominently.
    return (
        f"signal={sig} "
        f"parent_pid={parent_pid} "
        f"parent_name={parent_name} "
        f"loadavg_1m={load_str}"
        f"{extras_str} "
        f"parent_cmdline={parent_cmd!r}"
    )


def context_as_json(ctx: Dict[str, Any]) -> str:
    """JSON-serialise a context dict for structured ingestion.  Never raises."""
    try:
        return json.dumps(ctx, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"
