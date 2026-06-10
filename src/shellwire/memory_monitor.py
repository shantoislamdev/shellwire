"""Periodic process memory usage logging for Shellwire on Termux/Android.

Adapted from the Hermes gateway memory monitor
(hermes-agent/gateway/memory_monitor.py), itself ported from
cline/cline#10343 (src/standalone/memory-monitor.ts).

Shellwire is a lightweight Termux utility, so this module avoids the
``psutil`` dependency entirely.  Instead it reads RSS directly from
``/proc/self/status`` (always available on Linux/Android), with a
stdlib ``resource.getrusage`` fallback.

Key differences from the Hermes version:
  * PRIMARY RSS source: ``/proc/self/status`` → ``VmRSS:`` (kB → MB).
  * FALLBACK: ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` (checked >0
    because some Android kernels report 0).
  * No ``psutil`` fallback — keeps shellwire dependency-free.
  * Default interval: 600s (10 min) to reduce background wake-ups on
    Android / battery-constrained environments.
  * Logs open file descriptor count via ``/proc/self/fd`` (Termux-safe).
  * Thread name: ``shellwire-memory-monitor``.

The ``[MEMORY]`` prefix format is preserved for grep-ability across
both Hermes and Shellwire logs.

Public API (same as Hermes):
  * ``start_memory_monitoring(interval_seconds=600.0) -> bool``
  * ``stop_memory_monitoring(timeout=2.0) -> None``
  * ``log_memory_usage(prefix="") -> None``
  * ``is_running() -> bool``
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_KB_TO_MB = 1024  # VmRSS is reported in kB

_monitor_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_start_time: Optional[float] = None
_interval_seconds: float = 600.0  # 10 minutes — gentler on Android
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# RSS reading — Termux-optimised
# ---------------------------------------------------------------------------

def _get_rss_mb() -> Optional[int]:
    """Return current process RSS in MB, or None if unavailable.

    Strategy (Termux-first):
      1. Parse ``/proc/self/status`` for the ``VmRSS:`` field (kB → MB).
         This is always present on Linux/Android and gives the *current*
         RSS — not the high-water mark.
      2. Fall back to ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` (KB on
         Linux).  Some Android kernels return 0, so we guard against that.
      3. Return ``None`` — no psutil, shellwire stays lightweight.
    """
    # --- Primary: /proc/self/status ---
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # Format: "VmRSS:    123456 kB\n"
                    parts = line.split()
                    if len(parts) >= 2:
                        rss_kb = int(parts[1])
                        return rss_kb // _KB_TO_MB
    except Exception:
        pass

    # --- Fallback: resource (stdlib) ---
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if maxrss > 0:
            # On Linux ru_maxrss is in KB (high-water mark, not current).
            return maxrss // 1024
    except Exception:
        pass

    # No psutil — shellwire is intentionally dependency-free.
    return None


# ---------------------------------------------------------------------------
# Open FD count — Termux-safe
# ---------------------------------------------------------------------------

def _get_open_fd_count() -> Optional[int]:
    """Return the number of open file descriptors for this process.

    Uses ``/proc/self/fd`` which is available on Termux.  Returns None
    if the procfs directory is unreadable (sandboxed environment, etc.).
    """
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_memory_usage(prefix: str = "") -> None:
    """Log current memory usage in a grep-friendly ``[MEMORY] ...`` line.

    Safe to call on-demand from any thread at important lifecycle
    moments (after shutdown, after context compression, etc.).

    Parameters
    ----------
    prefix
        Optional extra tag inserted after ``[MEMORY]`` — e.g.
        ``"baseline"``, ``"shutdown"``.
    """
    rss = _get_rss_mb()
    uptime = int(time.monotonic() - _start_time) if _start_time else 0

    # GC generation counts — cheap proxy for garbage pressure.
    try:
        gc_counts = gc.get_count()  # (gen0, gen1, gen2)
    except Exception:
        gc_counts = (0, 0, 0)

    # Thread count — correlate with thread leaks.
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = 0

    # Open file descriptors — catch FD leaks early.
    fd_count = _get_open_fd_count()
    fd_str = f"fds={fd_count}" if fd_count is not None else "fds=unavailable"

    tag = f"{prefix} " if prefix else ""
    if rss is None:
        logger.info(
            "[MEMORY] %srss=unavailable gc=%s threads=%d %s uptime=%ds",
            tag,
            gc_counts,
            thread_count,
            fd_str,
            uptime,
        )
    else:
        logger.info(
            "[MEMORY] %srss=%dMB gc=%s threads=%d %s uptime=%ds",
            tag,
            rss,
            gc_counts,
            thread_count,
            fd_str,
            uptime,
        )


# ---------------------------------------------------------------------------
# Monitor thread
# ---------------------------------------------------------------------------

def _monitor_loop(stop_event: threading.Event, interval: float) -> None:
    """Background thread body — log every ``interval`` seconds until stopped."""
    while not stop_event.wait(interval):
        try:
            log_memory_usage()
        except Exception as e:
            # Never let the monitor crash shellwire; just log and carry on.
            logger.debug("Memory monitor iteration failed: %s", e)


def start_memory_monitoring(interval_seconds: float = 600.0) -> bool:
    """Start periodic memory usage logging in a daemon thread.

    Logs immediately to capture a baseline, then every ``interval_seconds``.
    Safe to call multiple times — subsequent calls are no-ops while the
    first monitor is still running.

    Parameters
    ----------
    interval_seconds
        How often to log.  Default 600s (10 minutes), reduced from
        Hermes's 300s to be gentler on Android battery / wake-ups.

    Returns
    -------
    bool
        True if a fresh monitor thread was started, False if one was
        already running or if memory introspection isn't available.
    """
    global _monitor_thread, _stop_event, _start_time, _interval_seconds

    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return False

        # Sanity-check that we can read RSS at all.  If /proc/self/status
        # and resource both fail, no point spinning a thread that can only
        # log "rss=unavailable" forever — warn once and bail.
        if _get_rss_mb() is None:
            logger.warning(
                "[MEMORY] Memory monitoring unavailable: could not read "
                "process RSS from /proc/self/status or resource.getrusage "
                "— skipping periodic logging.",
            )
            return False

        _start_time = time.monotonic()
        _interval_seconds = float(interval_seconds)
        _stop_event = threading.Event()

        # Baseline snapshot before the loop starts.
        log_memory_usage(prefix="baseline")

        _monitor_thread = threading.Thread(
            target=_monitor_loop,
            args=(_stop_event, _interval_seconds),
            name="shellwire-memory-monitor",
            daemon=True,
        )
        _monitor_thread.start()

        logger.info(
            "[MEMORY] Periodic memory monitoring started (interval: %ds)",
            int(_interval_seconds),
        )
        return True


def stop_memory_monitoring(timeout: float = 2.0) -> None:
    """Stop the monitor thread and log a final snapshot.

    Safe to call even if ``start_memory_monitoring()`` was never called.
    """
    global _monitor_thread, _stop_event

    with _lock:
        if _stop_event is None or _monitor_thread is None:
            return

        # Final snapshot before teardown so "last RSS" is always in the log.
        try:
            log_memory_usage(prefix="shutdown")
        except Exception:
            pass

        _stop_event.set()
        thread = _monitor_thread
        _monitor_thread = None
        _stop_event = None

    # Join outside the lock so a stuck log call can't deadlock shutdown.
    try:
        thread.join(timeout=timeout)
    except Exception:
        pass

    logger.info("[MEMORY] Periodic memory monitoring stopped")


def is_running() -> bool:
    """True if the background monitor thread is alive."""
    with _lock:
        return _monitor_thread is not None and _monitor_thread.is_alive()
