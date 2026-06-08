"""Tests for shellwire.output — truncation and ANSI stripping."""

from __future__ import annotations

from shellwire.output import OutputConfig, strip_ansi, truncate_output


# -----------------------------------------------------------------------
# truncate_output
# -----------------------------------------------------------------------


class TestTruncateOutput:
    """Tests for truncate_output."""

    def test_below_limit_returns_unchanged(self) -> None:
        text = "hello world"
        result, was_truncated = truncate_output(text, max_chars=100)
        assert result == text
        assert was_truncated is False

    def test_exact_limit_returns_unchanged(self) -> None:
        text = "x" * 100
        result, was_truncated = truncate_output(text, max_chars=100)
        assert result == text
        assert was_truncated is False

    def test_above_limit_produces_head_tail_with_notice(self) -> None:
        text = "A" * 200
        result, was_truncated = truncate_output(text, max_chars=100)
        assert was_truncated is True
        assert "output truncated" in result
        assert result.startswith("A" * 40)
        assert result.endswith("A" * 60)

    def test_preserves_40_60_ratio(self) -> None:
        text = "X" * 1000
        max_chars = 500
        result, was_truncated = truncate_output(text, max_chars=max_chars)
        assert was_truncated is True

        head_len = int(max_chars * 0.4)  # 200
        tail_len = max_chars - head_len  # 300

        # Head should be the first 200 chars.
        assert result[:head_len] == "X" * head_len
        # Tail should be the last 300 chars.
        assert result[-tail_len:] == "X" * tail_len
        # Middle should contain the truncation notice.
        middle = result[head_len:-tail_len]
        assert "output truncated" in middle
        assert str(len(text)) in middle  # Total length mentioned.

    def test_empty_string_below_limit(self) -> None:
        result, was_truncated = truncate_output("", max_chars=100)
        assert result == ""
        assert was_truncated is False

    def test_default_max_chars(self) -> None:
        text = "y" * 50_000
        result, was_truncated = truncate_output(text)
        assert was_truncated is False
        assert result == text


# -----------------------------------------------------------------------
# strip_ansi
# -----------------------------------------------------------------------


class TestStripAnsi:
    """Tests for strip_ansi."""

    def test_removes_csi_sequences(self) -> None:
        # SGR color: ESC [ 31 m
        text = "\x1b[31mred\x1b[0m"
        assert strip_ansi(text) == "red"

    def test_removes_cursor_movement(self) -> None:
        # Cursor up: ESC [ 2 A
        text = "\x1b[2Ahello"
        assert strip_ansi(text) == "hello"

    def test_removes_osc_sequences(self) -> None:
        # OSC title: ESC ] 0 ; title BEL
        text = "\x1b]0;my title\x07some text"
        assert strip_ansi(text) == "some text"

    def test_removes_osc_with_st_terminator(self) -> None:
        # OSC with ST (ESC \) terminator.
        text = "\x1b]0;my title\x1b\\some text"
        assert strip_ansi(text) == "some text"

    def test_clean_text_fast_path(self) -> None:
        text = "plain text without any escapes"
        assert strip_ansi(text) == text

    def test_empty_string(self) -> None:
        assert strip_ansi("") == ""

    def test_removes_8bit_csi(self) -> None:
        # 8-bit CSI (0x9b) followed by SGR.
        text = "\x9b31mred\x9b0m"
        assert strip_ansi(text) == "red"

    def test_mixed_escapes_and_text(self) -> None:
        text = "\x1b[1mbold\x1b[0m and \x1b[32mgreen\x1b[0m"
        assert strip_ansi(text) == "bold and green"

    def test_multiline_with_escapes(self) -> None:
        text = "\x1b[31mline1\x1b[0m\n\x1b[32mline2\x1b[0m"
        assert strip_ansi(text) == "line1\nline2"


# -----------------------------------------------------------------------
# OutputConfig
# -----------------------------------------------------------------------


class TestOutputConfig:
    """Tests for OutputConfig dataclass."""

    def test_default_values(self) -> None:
        config = OutputConfig()
        assert config.max_output_chars == 50_000

    def test_custom_values(self) -> None:
        config = OutputConfig(max_output_chars=10_000)
        assert config.max_output_chars == 10_000
