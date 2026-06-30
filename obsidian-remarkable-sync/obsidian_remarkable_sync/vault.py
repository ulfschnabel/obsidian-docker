from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pathspec


@dataclass
class NoteRecord:
    relative_path: str
    absolute_path: Path
    size_bytes: int
    modified_at: datetime


def _load_ignore_spec(vault_root: Path) -> pathspec.PathSpec | None:
    ignore_file = vault_root / ".obsidianignore"
    if not ignore_file.exists():
        return None
    patterns = ignore_file.read_text().splitlines()
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def enumerate_notes(vault_root: Path) -> list[NoteRecord]:
    if not vault_root.exists():
        raise FileNotFoundError(f"ERROR: Vault path not found: {vault_root}")
    if not vault_root.is_dir():
        raise NotADirectoryError(f"ERROR: Vault path is not a directory: {vault_root}")

    ignore_spec = _load_ignore_spec(vault_root)
    records: list[NoteRecord] = []

    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Skip hidden directories in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            if not filename.endswith(".md"):
                continue

            abs_path = Path(dirpath) / filename
            rel_path = str(abs_path.relative_to(vault_root))

            if ignore_spec and ignore_spec.match_file(rel_path):
                continue

            stat = abs_path.stat()
            records.append(NoteRecord(
                relative_path=rel_path,
                absolute_path=abs_path,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            ))

    return records
