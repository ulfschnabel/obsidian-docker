import pytest
from pathlib import Path

from obsidian_remarkable_sync.vault import enumerate_notes


def test_finds_nested_notes(vault):
    notes = enumerate_notes(vault)
    rel_paths = {n.relative_path for n in notes}
    assert "note1.md" in rel_paths
    assert "subdir/note2.md" in rel_paths
    assert "subdir/deep/note3.md" in rel_paths


def test_skips_hidden_dirs(vault):
    notes = enumerate_notes(vault)
    rel_paths = {n.relative_path for n in notes}
    assert not any(".obsidian" in p for p in rel_paths)
    assert not any(".trash" in p for p in rel_paths)


def test_returns_metadata(vault):
    notes = enumerate_notes(vault)
    note = next(n for n in notes if n.relative_path == "note1.md")
    assert note.absolute_path == vault / "note1.md"
    assert note.size_bytes > 0
    assert note.modified_at is not None


def test_obsidianignore_respected(vault):
    (vault / ".obsidianignore").write_text("subdir/\n")
    notes = enumerate_notes(vault)
    rel_paths = {n.relative_path for n in notes}
    assert "note1.md" in rel_paths
    assert "subdir/note2.md" not in rel_paths
    assert "subdir/deep/note3.md" not in rel_paths


def test_no_ignore_file_ok(vault):
    # No .obsidianignore — should still work
    notes = enumerate_notes(vault)
    assert len(notes) == 3


def test_invalid_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="Vault path not found"):
        enumerate_notes(tmp_path / "does_not_exist")


def test_empty_vault(tmp_path):
    notes = enumerate_notes(tmp_path)
    assert notes == []
