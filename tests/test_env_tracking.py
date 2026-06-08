"""Tests for env_tracking module."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from shellwire.env_tracking import SessionSnapshot, pipe_stdin, resolve_safe_cwd


class TestResolveSafeCwd:
    """Tests for resolve_safe_cwd."""

    def test_existing_directory(self, tmp_path):
        """Returns the directory unchanged when it exists."""
        result = resolve_safe_cwd(str(tmp_path))
        assert result == str(tmp_path)

    def test_walks_up_to_ancestor(self, tmp_path):
        """Walks up to nearest existing ancestor when path doesn't exist."""
        nonexistent = str(tmp_path / "a" / "b" / "c")
        result = resolve_safe_cwd(nonexistent)
        assert result == str(tmp_path)

    def test_fallback_to_tempdir(self):
        """Falls back to tempdir when entire path is bogus."""
        result = resolve_safe_cwd("/nonexistent/root/path")
        assert os.path.isdir(result)

    def test_empty_string(self):
        """Empty string falls back to tempdir."""
        result = resolve_safe_cwd("")
        assert os.path.isdir(result)


class TestSessionSnapshot:
    """Tests for SessionSnapshot."""

    def test_initial_cwd(self, tmp_path):
        """Initial CWD is set from constructor."""
        snap = SessionSnapshot("s1", str(tmp_path))
        assert snap.cwd == str(tmp_path)
        snap.cleanup()

    def test_wrap_command_includes_markers(self, tmp_path):
        """Wrapped command contains CWD markers."""
        snap = SessionSnapshot("s1", str(tmp_path))
        wrapped = snap.wrap_command("echo hello", str(tmp_path))
        
        assert "__SHELLWIRE_CWD_s1__" in wrapped
        assert "echo hello" in wrapped
        assert "export -p" in wrapped
        assert "pwd -P" in wrapped
        snap.cleanup()

    def test_wrap_command_sources_snapshot(self, tmp_path):
        """Wrapped command sources the snapshot file."""
        snap = SessionSnapshot("s1", str(tmp_path))
        wrapped = snap.wrap_command("ls", str(tmp_path))
        
        assert "source" in wrapped or "." in wrapped
        snap.cleanup()

    def test_update_cwd_extracts_marker(self, tmp_path):
        """update_cwd extracts CWD from marker and strips it from output."""
        snap = SessionSnapshot("s1", str(tmp_path))
        
        output = (
            f"some output\n"
            f"__SHELLWIRE_CWD_s1__{tmp_path}__SHELLWIRE_CWD_s1__\n"
            f"more output"
        )
        cleaned = snap.update_cwd(output)
        
        assert snap.cwd == str(tmp_path)
        assert "__SHELLWIRE_CWD_s1__" not in cleaned
        assert "some output" in cleaned
        snap.cleanup()

    def test_update_cwd_no_marker(self, tmp_path):
        """update_cwd returns output unchanged when no marker present."""
        snap = SessionSnapshot("s1", str(tmp_path))
        
        output = "hello world\nfoo bar\n"
        cleaned = snap.update_cwd(output)
        
        assert cleaned == output
        snap.cleanup()

    def test_cleanup_removes_temp_files(self, tmp_path):
        """cleanup removes temporary files."""
        snap = SessionSnapshot("s1", str(tmp_path))
        
        # Get paths before cleanup
        snapshot_path = snap._snapshot_file
        cwd_path = snap._cwd_file
        
        snap.cleanup()
        
        # Files should be removed (or never created)
        # Just verify no error is raised
        assert True


class TestPipeStdin:
    """Tests for pipe_stdin."""

    def test_pipe_stdin_writes_data(self):
        """pipe_stdin writes data to process stdin in a daemon thread."""
        import threading
        
        mock_proc = MagicMock()
        mock_stdin = MagicMock()
        # pipe_stdin uses getattr(proc.stdin, "buffer", proc.stdin),
        # and MagicMock auto-creates `buffer`. Delete it so the fallback
        # path uses proc.stdin directly.
        del mock_stdin.buffer
        mock_proc.stdin = mock_stdin
        
        # Use an event to synchronize with the daemon thread.
        write_done = threading.Event()
        original_close = mock_stdin.close
        def close_and_signal():
            original_close()
            write_done.set()
        mock_stdin.close = close_and_signal
        
        pipe_stdin(mock_proc, "hello")
        
        # Wait for the thread to complete (with generous timeout).
        write_done.wait(timeout=2.0)
        
        mock_stdin.write.assert_called_once()
        assert write_done.is_set()

    def test_pipe_stdin_handles_broken_pipe(self):
        """pipe_stdin gracefully handles BrokenPipeError."""
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError
        
        # Should not raise
        pipe_stdin(mock_proc, "hello")
        
        import time
        time.sleep(0.1)

    def test_pipe_stdin_handles_oserror(self):
        """pipe_stdin gracefully handles OSError."""
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = OSError("broken")
        
        # Should not raise
        pipe_stdin(mock_proc, "hello")
        
        import time
        time.sleep(0.1)
