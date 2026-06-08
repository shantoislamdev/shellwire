# This file contains modified third-party code licensed under the MIT License. See NOTICE for details.
"""Output processing utilities for shellwire.

Provides output truncation with head/tail split and client-side ANSI
escape stripping.  The ANSI regex is an ECMA-48 compliant pattern
ported from an ECMA-48 reference implementation.

Note: ``strip_ansi`` is a **client-side utility only** — shellwire
itself does not call it on streamed output.  It is exported for
downstream consumers that want to clean terminal output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# ANSI escape stripping (ECMA-48 compliant)
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
        r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"     # CSI sequence
        r"|\][\s\S]*?(?:\x07|\x1b\\)"                  # OSC (BEL or ST terminator)
        r"|[PX^_][\s\S]*?(?:\x1b\\)"                   # DCS/SOS/PM/APC strings
        r"|[\x20-\x2f]+[\x30-\x7e]"                    # nF escape sequences
        r"|[\x30-\x7e]"                                 # Fp/Fe/Fs single-byte
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"       # 8-bit CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"                       # 8-bit OSC
    r"|[\x80-\x9f]",                                    # Other 8-bit C1 controls
    re.DOTALL,
)

_HAS_ESCAPE = re.compile(r"[\x1b\x80-\x9f]")


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from *text*.

    Uses a fast-path check — if the string contains no escape-introducing
    bytes the regex substitution is skipped entirely.

    This is a **client-side utility** exported for consumers that want
    cleaned terminal output.  Shellwire itself never calls this on
    streamed output.
    """
    if not _HAS_ESCAPE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------

_TRUNCATION_NOTICE = (
    "\n\n--- [shellwire] output truncated "
    "({total} chars, showing first {head_len} + last {tail_len}) ---\n\n"
)


def truncate_output(
    text: str,
    max_chars: int = 50_000,
) -> tuple[str, bool]:
    """Truncate *text* to *max_chars* using a 40% head / 60% tail split.

    Returns:
        A ``(truncated_text, was_truncated)`` tuple.  If the text is
        within the limit, ``was_truncated`` is ``False`` and the
        original string is returned unchanged.
    """
    if len(text) <= max_chars:
        return text, False

    head_len = int(max_chars * 0.4)
    tail_len = max_chars - head_len

    notice = _TRUNCATION_NOTICE.format(
        total=len(text),
        head_len=head_len,
        tail_len=tail_len,
    )

    truncated = text[:head_len] + notice + text[-tail_len:]
    return truncated, True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class OutputConfig:
    """Configuration for output processing."""

    max_output_chars: int = 50_000
