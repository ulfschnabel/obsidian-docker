"""obsidian-vault-mirror: a read-only file copy of the vault, projected from CouchDB (design D7, D9).

The mirror owns its target directory: it writes each live note at its path
(atomically, with the document's mtime), removes deleted notes and emptied
folders, and on a rebuild removes every file that is not a live note. It
only ever reads from CouchDB. Consumers (reMarkable sync, backups) mount the
directory read-only.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx

from vaultstore import format as fmt
from vaultstore.errors import IncompatibleVault
from vaultstore.follower import CheckpointFile, Follower, FollowerFailed
from vaultstore.store import Note

log = logging.getLogger("obsidian-vault-mirror")

TMP_PREFIX = ".mirror-tmp-"


def safe_relative(path: str) -> str | None:
    """`path` if it stays inside the mirror root when joined to it, else None."""
    if not path or path.startswith("/") or "\x00" in path:
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


class Mirror:
    """A Projection writing notes as files under `root`."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, set[str]] = {}  # note id -> files on disk for it
        self._scan()

    # --- Projection ----------------------------------------------------------------------

    def apply(self, note: Note) -> None:
        rel = safe_relative(note.path)
        if rel is None:
            log.error("not mirroring %s: unsafe path %r", note.id, note.path)
            return
        target = self.root / rel
        data = fmt.file_bytes(note.content)
        mtime_ns = note.mtime * 1_000_000
        try:
            if self._unchanged(target, data):
                if target.stat().st_mtime_ns != mtime_ns:
                    os.utime(target, ns=(mtime_ns, mtime_ns))
            else:
                self._write(target, data, mtime_ns)
        except (IsADirectoryError, NotADirectoryError, FileExistsError) as e:
            log.error("not mirroring %s at %r: the path collides with another file or folder (%s)", note.id, rel, e)
            return
        for old in self._files.get(note.id, set()) - {rel}:  # its previous path, e.g. other letter case
            old_path = self.root / old
            if old_path.exists() and not os.path.samefile(old_path, target):
                self._unlink(old)
        self._files[note.id] = {rel}

    def remove(self, doc_id: str, revision: str | None = None) -> None:
        for rel in self._files.pop(doc_id, set()):
            self._unlink(rel)

    def reconcile(self, live_ids: frozenset[str]) -> None:
        self._scan()
        for doc_id in [i for i in self._files if i not in live_ids]:
            log.info("removing %s: not a live note", sorted(self._files[doc_id]))
            self.remove(doc_id)
        for dirpath, _, _ in sorted(os.walk(self.root), key=lambda w: len(w[0]), reverse=True):
            if Path(dirpath) != self.root and not os.listdir(dirpath):
                os.rmdir(dirpath)

    # --- files -----------------------------------------------------------------------------

    def _scan(self) -> None:
        """Index the files on disk by note id; remove temporary files a crash left behind."""
        self._files = {}
        for rel in self._walk():
            self._files.setdefault(fmt.path2id(rel), set()).add(rel)

    def _walk(self) -> Iterator[str]:
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                full = Path(dirpath, name)
                if name.startswith(TMP_PREFIX):
                    full.unlink(missing_ok=True)
                    continue
                yield full.relative_to(self.root).as_posix()

    @staticmethod
    def _unchanged(target: Path, data: bytes) -> bool:
        try:
            return target.is_file() and target.stat().st_size == len(data) and target.read_bytes() == data
        except FileNotFoundError:
            return False

    def _write(self, target: Path, data: bytes, mtime_ns: int) -> None:
        """Temporary file, fsync, mtime, rename: readers see the old file or the new one, never a part."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f"{TMP_PREFIX}{uuid.uuid4().hex[:12]}"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.utime(tmp, ns=(mtime_ns, mtime_ns))
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _unlink(self, rel: str) -> None:
        path = self.root / rel
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self.root:
            try:
                parent.rmdir()
            except OSError:  # not empty, or already gone
                break
            parent = parent.parent


# --- service -------------------------------------------------------------------------------

REQUIRED = ("COUCHDB_URL", "COUCHDB_USER", "COUCHDB_PASSWORD")


def main(env: Mapping[str, str] = os.environ) -> int:
    """Run until stopped. Returns 1 when CouchDB stays unavailable or the vault is unreadable,
    so the container restarts and resumes from its checkpoint; 2 when misconfigured."""
    logging.basicConfig(level=env.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        log.error("missing settings: %s", ", ".join(missing))
        return 2
    root = Path(env.get("MIRROR_ROOT", "/mirror"))
    state = Path(env.get("STATE_DIR", "/state")) / "mirror.json"
    budget = float(env.get("RETRY_BUDGET_S", "300"))
    with httpx.Client(base_url=env["COUCHDB_URL"], auth=(env["COUCHDB_USER"], env["COUCHDB_PASSWORD"]), timeout=60) as c:
        follower = Follower(c, Mirror(root), CheckpointFile(state), retry_budget_s=budget)
        try:
            follower.run(threading.Event())
        except (FollowerFailed, IncompatibleVault) as e:
            log.error("%s; exiting so the container restarts", e)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
