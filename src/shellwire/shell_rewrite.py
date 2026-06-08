# This file contains modified third-party code licensed under the MIT License. See NOTICE for details.
"""Shell command rewriting for correct compound-background semantics.

Ensures commands correctly run in background.

When a user writes ``A && B &``, bash only backgrounds ``B``, not the
entire compound.  This module rewrites such commands to
``A && { B & }`` so that the trailing ``&`` explicitly scopes to the
last simple command in the chain, which is what users typically intend.
"""

from __future__ import annotations


def _read_shell_token(command: str, start: int) -> tuple[str, int]:
    """Read one shell token from *command* starting at index *start*.

    Handles single-quoted strings (no escape processing), double-quoted
    strings (backslash escapes ``\\``, ``\"``, ``\\$``, ``\\```, ``\\!``),
    and unquoted tokens (terminated by whitespace or shell metacharacters).

    Returns:
        ``(token, end_index)`` where *end_index* is the position
        immediately after the consumed token.
    """
    length = len(command)
    if start >= length:
        return "", start

    ch = command[start]

    # Single-quoted string — no escape processing.
    if ch == "'":
        end = command.find("'", start + 1)
        if end == -1:
            return command[start:], length
        return command[start : end + 1], end + 1

    # Double-quoted string — honour backslash escapes.
    if ch == '"':
        pos = start + 1
        token = '"'
        while pos < length:
            c = command[pos]
            if c == "\\":
                if pos + 1 < length:
                    token += command[pos : pos + 2]
                    pos += 2
                else:
                    token += "\\"
                    pos += 1
            elif c == '"':
                token += '"'
                pos += 1
                return token, pos
            else:
                token += c
                pos += 1
        return token, pos

    # Unquoted token — terminates at whitespace or metacharacter.
    pos = start
    token = ""
    while pos < length:
        c = command[pos]
        if c in " \t\n":
            break
        if c in ";|&(){}><#":
            if not token:
                # Return the metacharacter(s) as the token.
                token = c
                pos += 1
            break
        if c == "\\":
            if pos + 1 < length:
                token += command[pos : pos + 2]
                pos += 2
            else:
                token += "\\"
                pos += 1
        else:
            token += c
            pos += 1

    return token, pos


def rewrite_compound_background(command: str) -> str:
    """Rewrite ``A && B &`` to ``A && { B & }``.

    Scans the command string for chain operators (``&&``, ``||``) followed
    by a trailing ``&`` that backgrounds only the last simple command.
    Wraps the backgrounded segment in braces so the ``&`` explicitly
    scopes to it.

    If no rewrite is needed (no chain operator, no trailing ``&``, or
    already wrapped in braces), the command is returned unchanged.
    """
    length = len(command)
    pos = 0

    # Track positions of real background `&` and chain operators.
    background_amps: list[int] = []
    chain_ops: list[tuple[int, int]] = []  # (start, end) of && or ||
    paren_depth = 0
    brace_depth = 0

    while pos < length:
        # Skip whitespace.
        if command[pos] in " \t":
            pos += 1
            continue

        # Newline at depth 0 acts as a command separator (like `;`).
        if command[pos] == "\n" and paren_depth == 0 and brace_depth == 0:
            pos += 1
            continue

        # Comment — skip to end of line.
        if command[pos] == "#":
            nl = command.find("\n", pos)
            pos = nl + 1 if nl != -1 else length
            continue

        # Parentheses.
        if command[pos] == "(":
            paren_depth += 1
            pos += 1
            continue
        if command[pos] == ")":
            paren_depth = max(0, paren_depth - 1)
            pos += 1
            continue

        # Braces.
        if command[pos] == "{":
            brace_depth += 1
            pos += 1
            continue
        if command[pos] == "}":
            brace_depth = max(0, brace_depth - 1)
            pos += 1
            continue

        # Semicolons at depth 0 reset chain tracking.
        if command[pos] == ";" and paren_depth == 0 and brace_depth == 0:
            chain_ops.clear()
            background_amps.clear()
            pos += 1
            continue

        # `&&` or `||` chain operators.
        if (
            pos + 1 < length
            and paren_depth == 0
            and brace_depth == 0
        ):
            two = command[pos : pos + 2]
            if two == "&&" or two == "||":
                chain_ops.append((pos, pos + 2))
                pos += 2
                continue

        # `|` single pipe — reset chain (new pipeline segment).
        if (
            command[pos] == "|"
            and paren_depth == 0
            and brace_depth == 0
        ):
            # Make sure it's not `||` (already handled above).
            if pos + 1 >= length or command[pos + 1] != "|":
                chain_ops.clear()
                background_amps.clear()
                pos += 1
                continue

        # `&>` redirect — NOT a background operator.
        if command[pos] == "&" and pos + 1 < length and command[pos + 1] == ">":
            pos += 2
            continue

        # `>&` or `<&` fd duplication redirect — the `&` is part of redirect.
        if command[pos] == "&":
            if pos > 0 and command[pos - 1] in "><":
                pos += 1
                continue

        # Real background `&` detection.
        if (
            command[pos] == "&"
            and paren_depth == 0
            and brace_depth == 0
        ):
            # Ensure it's not `&&` (already handled above).
            if pos + 1 >= length or command[pos + 1] != "&":
                background_amps.append(pos)
                pos += 1
                continue

        # Any other token — read and skip.
        _, pos = _read_shell_token(command, pos)

    # ------------------------------------------------------------------
    # Apply rewrites back-to-front to preserve indices.
    # ------------------------------------------------------------------
    if not background_amps or not chain_ops:
        return command

    result = command
    # Process background `&` positions from right to left.
    for amp_pos in reversed(background_amps):
        # Find the nearest chain operator BEFORE this `&`.
        nearest_chain: tuple[int, int] | None = None
        for op_start, op_end in chain_ops:
            if op_end <= amp_pos:
                nearest_chain = (op_start, op_end)

        if nearest_chain is None:
            continue

        chain_end = nearest_chain[1]

        # Find the start of the segment after the chain operator
        # (skip whitespace).
        seg_start = chain_end
        while seg_start < len(result) and result[seg_start] in " \t":
            seg_start += 1

        # Check if the segment is already wrapped in braces.
        if seg_start < len(result) and result[seg_start] == "{":
            continue

        # Find the end of the segment (the `&` itself, plus any
        # trailing whitespace).
        seg_end = amp_pos
        # Include the `&` in what we wrap.
        amp_end = amp_pos + 1
        # Trim trailing whitespace between last token and `&`.
        seg_content_end = amp_pos
        while seg_content_end > seg_start and result[seg_content_end - 1] in " \t":
            seg_content_end -= 1

        # Build the replacement: `{ <segment> & }`
        segment = result[seg_start:seg_content_end]
        replacement = "{ " + segment + " & }"

        result = result[:seg_start] + replacement + result[amp_end:]

    return result
