from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)


class VaultWatcher:
    def __init__(
        self,
        vault_root: Path,
        sync_batch: Callable[[set[Path], set[Path]], None],
        debounce_seconds: float = 60.0,
    ) -> None:
        self._vault_root = vault_root
        self._sync_batch = sync_batch
        self._debounce_seconds = debounce_seconds

        self._changed: set[Path] = set()
        self._deleted: set[Path] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

        self._observer = Observer()
        self._handler = _Handler(self._on_event)
        self._observer.schedule(self._handler, str(vault_root), recursive=True)

    def start(self) -> None:
        self._observer.start()
        log.info("Watching vault at %s (debounce %.0fs)", self._vault_root, self._debounce_seconds)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        # Flush any pending batch
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._changed or self._deleted:
                log.info("Flushing pending batch on shutdown")
                self._fire()

    def _on_event(self, path: Path, deleted: bool) -> None:
        with self._lock:
            if deleted:
                self._deleted.add(path)
                self._changed.discard(path)
            else:
                self._changed.add(path)
                self._deleted.discard(path)

            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._trigger)
            self._timer.daemon = True
            self._timer.start()

    def _trigger(self) -> None:
        with self._lock:
            changed = self._changed.copy()
            deleted = self._deleted.copy()
            self._changed.clear()
            self._deleted.clear()
            self._timer = None
        if changed or deleted:
            log.info("Debounce elapsed — syncing %d changed, %d deleted", len(changed), len(deleted))
            self._sync_batch(changed, deleted)


class _Handler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[Path, bool], None]) -> None:
        self._callback = callback

    def _is_relevant(self, path_str: str) -> bool:
        path = Path(path_str)
        if not path.suffix == ".md":
            return False
        # Skip events inside hidden directories
        try:
            parts = path.parts
            return not any(part.startswith(".") for part in parts)
        except Exception:
            return False

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_relevant(event.src_path):
            self._callback(Path(event.src_path), False)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_relevant(event.src_path):
            self._callback(Path(event.src_path), False)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_relevant(event.src_path):
            self._callback(Path(event.src_path), True)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            if self._is_relevant(event.src_path):
                self._callback(Path(event.src_path), True)
            if self._is_relevant(event.dest_path):
                self._callback(Path(event.dest_path), False)
