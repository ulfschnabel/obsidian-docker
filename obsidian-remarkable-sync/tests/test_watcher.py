import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obsidian_remarkable_sync.watcher import VaultWatcher, _Handler


# --- _Handler relevance filter ---

def test_handler_ignores_non_md(tmp_path):
    cb = MagicMock()
    handler = _Handler(cb)

    ev = MagicMock()
    ev.is_directory = False
    ev.src_path = str(tmp_path / "file.txt")
    handler.on_modified(ev)
    cb.assert_not_called()


def test_handler_ignores_hidden_dir(tmp_path):
    cb = MagicMock()
    handler = _Handler(cb)

    ev = MagicMock()
    ev.is_directory = False
    ev.src_path = str(tmp_path / ".obsidian" / "config.md")
    handler.on_modified(ev)
    cb.assert_not_called()


def test_handler_fires_for_md(tmp_path):
    cb = MagicMock()
    handler = _Handler(cb)

    ev = MagicMock()
    ev.is_directory = False
    ev.src_path = str(tmp_path / "note.md")
    handler.on_modified(ev)
    cb.assert_called_once_with(Path(ev.src_path), False)


def test_handler_fires_delete(tmp_path):
    cb = MagicMock()
    handler = _Handler(cb)

    ev = MagicMock()
    ev.is_directory = False
    ev.src_path = str(tmp_path / "note.md")
    handler.on_deleted(ev)
    cb.assert_called_once_with(Path(ev.src_path), True)


# --- VaultWatcher debounce ---

def test_rapid_events_collapse_into_one_batch(tmp_path):
    batches = []

    def sync_batch(changed, deleted):
        batches.append((changed.copy(), deleted.copy()))

    watcher = VaultWatcher(tmp_path, sync_batch, debounce_seconds=0.1)
    note = tmp_path / "note.md"

    # Fire 5 events rapidly
    for _ in range(5):
        watcher._on_event(note, False)

    time.sleep(0.3)  # wait for debounce to fire

    assert len(batches) == 1
    assert note in batches[0][0]


def test_debounce_timer_resets_on_new_event(tmp_path):
    fire_times = []

    def sync_batch(changed, deleted):
        fire_times.append(time.monotonic())

    watcher = VaultWatcher(tmp_path, sync_batch, debounce_seconds=0.15)
    note = tmp_path / "note.md"

    t0 = time.monotonic()
    watcher._on_event(note, False)
    time.sleep(0.1)
    watcher._on_event(note, False)  # reset timer
    time.sleep(0.25)  # wait for second debounce

    assert len(fire_times) == 1
    # Should fire after t0 + 0.1 + 0.15 ≈ 0.25s, not at t0 + 0.15s
    assert fire_times[0] - t0 > 0.2


def test_delete_event_triggers_removal(tmp_path):
    batches = []

    def sync_batch(changed, deleted):
        batches.append((changed.copy(), deleted.copy()))

    watcher = VaultWatcher(tmp_path, sync_batch, debounce_seconds=0.1)
    note = tmp_path / "note.md"
    watcher._on_event(note, True)
    time.sleep(0.25)

    assert len(batches) == 1
    assert note in batches[0][1]
    assert note not in batches[0][0]
