from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

from obsidian_remarkable_sync.vault import NoteRecord

log = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = Path("/state/manifest.json")


class ManifestEntry(TypedDict):
    sha256: str
    remote_path: str
    synced_at: str


class Manifest:
    def __init__(self, path: Path = DEFAULT_MANIFEST_PATH) -> None:
        self._path = path
        self._data: dict[str, ManifestEntry] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not load manifest at %s: %s — starting fresh", self._path, e)
                self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def get(self, relative_path: str) -> ManifestEntry | None:
        return self._data.get(relative_path)

    def put(self, relative_path: str, sha256: str, remote_path: str) -> None:
        self._data[relative_path] = {
            "sha256": sha256,
            "remote_path": remote_path,
            "synced_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        self._save()

    def delete(self, relative_path: str) -> None:
        self._data.pop(relative_path, None)
        self._save()

    def reset(self) -> None:
        self._data = {}
        if self._path.exists():
            self._path.unlink()


def compute_hash(file: Path) -> str:
    h = hashlib.sha256()
    h.update(file.read_bytes())
    return h.hexdigest()


def classify_note(
    note: NoteRecord, manifest: Manifest
) -> Literal["new", "modified", "unchanged"]:
    entry = manifest.get(note.relative_path)
    if entry is None:
        return "new"
    current_hash = compute_hash(note.absolute_path)
    if current_hash != entry["sha256"]:
        return "modified"
    return "unchanged"
