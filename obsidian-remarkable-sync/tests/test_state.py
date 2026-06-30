from pathlib import Path

import pytest

from obsidian_remarkable_sync.state import Manifest, classify_note, compute_hash
from obsidian_remarkable_sync.vault import NoteRecord
from datetime import datetime, timezone


def make_record(tmp_path, name="note.md", content="hello"):
    p = tmp_path / name
    p.write_text(content)
    stat = p.stat()
    return NoteRecord(
        relative_path=name,
        absolute_path=p,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def test_new_note_classified(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    note = make_record(tmp_path)
    assert classify_note(note, m) == "new"


def test_unchanged_note_classified(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    note = make_record(tmp_path)
    h = compute_hash(note.absolute_path)
    m.put(note.relative_path, h, "/Obsidian/note")
    assert classify_note(note, m) == "unchanged"


def test_modified_note_classified(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    note = make_record(tmp_path)
    m.put(note.relative_path, "oldhash", "/Obsidian/note")
    assert classify_note(note, m) == "modified"


def test_manifest_persists_across_instances(tmp_path):
    path = tmp_path / "manifest.json"
    m1 = Manifest(path)
    m1.put("note.md", "abc123", "/Obsidian/note")

    m2 = Manifest(path)
    entry = m2.get("note.md")
    assert entry is not None
    assert entry["sha256"] == "abc123"
    assert entry["remote_path"] == "/Obsidian/note"


def test_failed_upload_does_not_update_manifest(tmp_path):
    # Simulate: classify note, decide to upload, upload fails (caller does NOT call put)
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    note = make_record(tmp_path)
    # Do NOT call m.put — simulating a failed upload
    assert classify_note(note, m) == "new"  # still new on next classification


def test_reset_deletes_file(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.put("note.md", "abc", "/Obsidian/note")
    assert path.exists()
    m.reset()
    assert not path.exists()
    assert m.get("note.md") is None


def test_delete_removes_entry(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.put("note.md", "abc", "/Obsidian/note")
    m.delete("note.md")
    assert m.get("note.md") is None
