"""Tests for shellwire.shell_rewrite — compound-background rewriting."""

from __future__ import annotations

from shellwire.shell_rewrite import rewrite_compound_background


class TestRewriteCompoundBackground:
    """Tests for rewrite_compound_background."""

    def test_simple_background_not_rewritten(self) -> None:
        """A simple ``cmd &`` with no chain operator is unchanged."""
        assert rewrite_compound_background("sleep 10 &") == "sleep 10 &"

    def test_and_chain_background(self) -> None:
        """``A && B &`` → ``A && { B & }``."""
        result = rewrite_compound_background("A && B &")
        assert result == "A && { B & }"

    def test_or_chain_background(self) -> None:
        """``A || B &`` → ``A || { B & }``."""
        result = rewrite_compound_background("A || B &")
        assert result == "A || { B & }"

    def test_triple_chain_background(self) -> None:
        """``A && B && C &`` rewrites the last segment."""
        result = rewrite_compound_background("A && B && C &")
        assert result == "A && B && { C & }"

    def test_semicolons_reset_chain(self) -> None:
        """Semicolons separate independent commands — no chain across them."""
        result = rewrite_compound_background("A; B &")
        assert result == "A; B &"

    def test_quoted_ampersand_not_rewritten(self) -> None:
        """``&`` inside quotes is not a shell operator."""
        cmd = 'echo "A && B &"'
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_single_quoted_ampersand_not_rewritten(self) -> None:
        """``&`` inside single quotes is not a shell operator."""
        cmd = "echo 'A && B &'"
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_parenthesized_subshell_skipped(self) -> None:
        """Operators inside ``(...)`` are at non-zero paren depth."""
        cmd = "(A && B &)"
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_redirect_ampersand_not_background(self) -> None:
        """``&>`` is a redirect, not a background operator."""
        cmd = "A && B &>/dev/null"
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_existing_brace_group_idempotent(self) -> None:
        """Already-wrapped ``A && { B & }`` is not double-wrapped."""
        cmd = "A && { B & }"
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_no_trailing_ampersand_unchanged(self) -> None:
        """Commands without any ``&`` are returned as-is."""
        cmd = "A && B && C"
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_pipe_resets_chain(self) -> None:
        """A single ``|`` pipe starts a new pipeline segment."""
        cmd = "A | B &"
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_fd_redirect_ampersand_not_background(self) -> None:
        """``>&2`` is fd duplication, not background."""
        cmd = "A && echo err >&2"
        result = rewrite_compound_background(cmd)
        assert result == cmd

    def test_empty_command(self) -> None:
        assert rewrite_compound_background("") == ""

    def test_only_whitespace(self) -> None:
        assert rewrite_compound_background("   ") == "   "
