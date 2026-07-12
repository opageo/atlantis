"""Tests for the file browser component."""

from __future__ import annotations

from pathlib import Path

from atlantis.ui.components.file_browser import _human_size


class TestHumanSize:
    """Tests for the human-readable file size formatter."""

    def test_bytes(self, tmp_path: Path) -> None:
        """Sizes under 1 KB are shown in bytes."""
        f = tmp_path / "small.txt"
        f.write_bytes(b"x" * 500)
        assert _human_size(f) == "500 B"

    def test_kilobytes(self, tmp_path: Path) -> None:
        """Sizes in KB range."""
        f = tmp_path / "medium.bin"
        f.write_bytes(b"x" * 2048)
        assert _human_size(f) == "2 KB"

    def test_megabytes(self, tmp_path: Path) -> None:
        """Sizes in MB range."""
        f = tmp_path / "large.dat"
        f.write_bytes(b"x" * (2 * 1024 * 1024))
        assert _human_size(f) == "2 MB"

    def test_gigabytes(self, tmp_path: Path) -> None:
        """Sizes in GB range."""
        f = tmp_path / "huge.bin"
        f.write_bytes(b"x" * int(1.5 * 1024 * 1024 * 1024))
        assert _human_size(f) == "2 GB"

    def test_missing_file_returns_question(self, tmp_path: Path) -> None:
        """Non-existent path returns '?'."""
        f = tmp_path / "does_not_exist"
        assert _human_size(f) == "?"

    def test_zero_bytes(self, tmp_path: Path) -> None:
        """Zero-byte file."""
        f = tmp_path / "empty"
        f.write_bytes(b"")
        assert _human_size(f) == "0 B"
