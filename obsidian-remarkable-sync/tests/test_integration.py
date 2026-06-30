"""Integration test: patch high-level functions to avoid subprocess module conflicts."""
import time
from pathlib import Path
from unittest.mock import patch

from obsidian_remarkable_sync.main import run_full_sync
from obsidian_remarkable_sync.state import Manifest
from obsidian_remarkable_sync.watcher import VaultWatcher


def test_initial_sync_uploads_new_notes(vault, tmp_path):
    manifest = Manifest(tmp_path / "manifest.json")
    rmapi_config = tmp_path / "rmapi"
    rmapi_config.mkdir()
    (rmapi_config / "rmapi.conf").write_text("{}")

    uploaded = []

    def fake_upload(pdf, remote_path, *, rmapi_config):
        uploaded.append(remote_path)

    with patch("obsidian_remarkable_sync.main.convert_to_pdf"), \
         patch("obsidian_remarkable_sync.main.ensure_folder"), \
         patch("obsidian_remarkable_sync.main.upload_pdf", side_effect=fake_upload):
        run_full_sync(vault, manifest, rmapi_config, "Obsidian", "a5")

    # 3 notes in fixture → 3 uploads
    assert len(uploaded) == 3
    # Manifest updated for all notes
    assert manifest.get("note1.md") is not None
    assert manifest.get("subdir/note2.md") is not None


def test_watcher_picks_up_new_file_after_debounce(vault, tmp_path):
    batched = []

    def sync_batch(changed, deleted):
        batched.append((changed.copy(), deleted.copy()))

    watcher = VaultWatcher(vault, sync_batch, debounce_seconds=0.1)

    new_note = vault / "new_note.md"
    new_note.write_text("# New\nContent")
    watcher._on_event(new_note, False)

    time.sleep(0.3)

    assert len(batched) == 1
    assert new_note in batched[0][0]


def test_unchanged_notes_not_reuploaded(vault, tmp_path):
    manifest = Manifest(tmp_path / "manifest.json")
    rmapi_config = tmp_path / "rmapi"
    rmapi_config.mkdir()
    (rmapi_config / "rmapi.conf").write_text("{}")

    uploaded = []

    def fake_upload(pdf, remote_path, *, rmapi_config):
        uploaded.append(remote_path)

    with patch("obsidian_remarkable_sync.main.convert_to_pdf"), \
         patch("obsidian_remarkable_sync.main.ensure_folder"), \
         patch("obsidian_remarkable_sync.main.upload_pdf", side_effect=fake_upload):
        run_full_sync(vault, manifest, rmapi_config, "Obsidian", "a5")
        first_count = len(uploaded)
        # Second run — nothing changed
        run_full_sync(vault, manifest, rmapi_config, "Obsidian", "a5")

    assert len(uploaded) == first_count  # no new uploads
