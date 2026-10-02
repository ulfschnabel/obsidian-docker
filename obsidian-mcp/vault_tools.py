"""The Obsidian MCP's tools on the LiveSync document store (design D3–D5, D8).

CouchDB is the only copy the MCP touches. Reads go to CouchDB; listing,
search, backlinks, tags and path resolution use an in-memory catalog that a
change-feed follower keeps current. Every mutation is conditional on a
revision, and reports success only once CouchDB has acknowledged it.

This module holds no transport (FastMCP, OAuth) and no embedding model, so
it can be tested directly; server.py wraps it.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import replace

from vaultstore import format as fmt
from vaultstore.errors import (
    AmbiguousMatch, Conflict, Exists, InvalidPath, NoMatch, NotFound, StoreUnavailable, VaultError,
)
from vaultstore.follower import Follower
from vaultstore.store import Note, Store

MAX_ATTEMPTS = 5  # read-modify-write attempts for append, replace and link rewrites
_FORBIDDEN = frozenset(':\\*"<>|?')  # Obsidian forbids these; the LiveSync core mangles ':'


# --- paths (D8) -------------------------------------------------------------------------

def _check(path: str, what: str) -> None:
    if not path:
        raise InvalidPath(f"empty {what}")
    if path.startswith("/"):
        raise InvalidPath(f"{path!r}: use a vault-relative path, not an absolute one")
    bad = sorted({c for c in path if c in _FORBIDDEN or ord(c) < 0x20})
    if bad:
        raise InvalidPath(f"{path!r} contains {''.join(bad)!r}; Obsidian does not allow that in names")
    for segment in path.split("/"):
        if not segment:
            raise InvalidPath(f"{path!r} has an empty path segment")
        if segment.startswith("."):
            raise InvalidPath(f"{path!r}: names starting with '.' (hidden folders, '..') are not allowed")


def note_path(path: str) -> str:
    """Validate a note path; `.md` is appended when missing."""
    _check(path, "note path")
    return path if path.lower().endswith(".md") else path + ".md"


def folder_path(path: str, *, allow_root: bool = False) -> str:
    """Validate a folder path; one trailing '/' is ignored, and '' is the vault root if allowed."""
    path = path[:-1] if path.endswith("/") else path
    if path == "" and allow_root:
        return ""
    _check(path, "folder path")
    return path


def name_segment(name: str) -> str:
    """Validate a new name for rename_note/rename_folder: one path segment."""
    if "/" in name:
        raise InvalidPath(f"{name!r}: give a new name, not a path; use move_note or move_folder to move")
    _check(name, "name")
    return name


def path_key(path: str) -> str:
    """Paths that differ only in Unicode form or letter case (as LiveSync's ids ignore case) share a key."""
    return unicodedata.normalize("NFC", path).lower()


def _nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _is_markdown(note: Note) -> bool:
    return note.type == "plain" and isinstance(note.content, str) and note.path.lower().endswith(".md")


# --- wikilinks ---------------------------------------------------------------------------

def _link_target(path: str) -> tuple[str, str]:
    """(path, name) as a wikilink writes them: notes without `.md`, attachments with their extension."""
    target = path[:-3] if path.lower().endswith(".md") else path
    return target, target.rsplit("/", 1)[-1]


def _link_rules(moves: Iterable[tuple[str, str]]) -> list[tuple[re.Pattern, Callable[[re.Match], str]]]:
    """The pre-CouchDB server's rules: path-qualified links, then bare names when the name changed."""
    rules = []
    for old, new in moves:
        old_path, old_name = _link_target(old)
        new_path, new_name = _link_target(new)
        rules.append((
            re.compile(r"\[\[" + re.escape(old_path) + r"(\|[^\]]*)?]]", re.IGNORECASE),
            lambda m, to=new_path: f"[[{to}{m.group(1) or ''}]]",
        ))
        if old_name.lower() != new_name.lower():
            rules.append((
                re.compile(r"\[\[" + re.escape(old_name) + r"(\|[^\]]*)?]]", re.IGNORECASE),
                lambda m, to=new_name: f"[[{to}{m.group(1) or ''}]]",
            ))
    return rules


def _apply_rules(rules, text: str) -> str:
    for pattern, repl in rules:
        text = pattern.sub(repl, text)
    return text


def rewrite_links(text: str, moves: Iterable[tuple[str, str]]) -> str:
    """Rewrite [[wikilinks]] in `text` for (old path, new path) moves."""
    return _apply_rules(_link_rules(moves), text)


# --- catalog (D5) ------------------------------------------------------------------------

def _generation(revision: str) -> int:
    return int(revision.split("-", 1)[0])


class Catalog:
    """Live notes in memory, kept current by a Follower; resolves, lists and searches.

    The MCP's own writes are recorded at once (read-your-writes) and protected
    for `protect_s` against older feed events that were already in flight.
    Attachments are kept as metadata only.
    """

    def __init__(self, *, protect_s: float = 60, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.RLock()
        self._notes: dict[str, Note] = {}
        self._keys: dict[str, set[str]] = {}
        self._floors: dict[str, tuple[int, float]] = {}  # id -> (generation we wrote, until)
        self._loaded = threading.Event()
        self._protect = protect_s
        self._clock = clock

    @property
    def loaded(self) -> bool:
        return self._loaded.is_set()

    # Projection: driven by the follower.

    def apply(self, note: Note) -> None:
        with self._lock:
            if not self._outdated(note.id, note.revision):
                self._put(note)

    def remove(self, doc_id: str, revision: str | None = None) -> None:
        with self._lock:
            if not self._outdated(doc_id, revision):
                self._drop(doc_id)

    def reconcile(self, live_ids: frozenset[str]) -> None:
        with self._lock:
            for doc_id in [i for i in self._notes if i not in live_ids]:
                self._drop(doc_id)
        self._loaded.set()

    # The MCP's own acknowledged writes.

    def wrote(self, note: Note) -> None:
        with self._lock:
            self._floors[note.id] = (_generation(note.revision), self._clock() + self._protect)
            self._put(note)

    def deleted(self, doc_id: str, revision: str) -> None:
        with self._lock:
            self._floors[doc_id] = (_generation(revision), self._clock() + self._protect)
            self._drop(doc_id)

    # Queries.

    def get(self, doc_id: str) -> Note | None:
        with self._lock:
            return self._notes.get(doc_id)

    def snapshot(self) -> list[Note]:
        with self._lock:
            return sorted(self._notes.values(), key=lambda n: n.path)

    def resolve(self, path: str) -> str | None:
        """The stored path of the live note `path` names, or None."""
        with self._lock:
            exact = self._notes.get(fmt.path2id(path))
            if exact is not None:
                return exact.path
            ids = self._keys.get(path_key(path), set())
            if len(ids) > 1:
                paths = sorted(self._notes[i].path for i in ids)
                raise AmbiguousMatch(f"{path!r} matches notes that differ only in Unicode form: {paths}; give one exactly")
            return self._notes[next(iter(ids))].path if ids else None

    # Internals; the lock is held.

    def _outdated(self, doc_id: str, revision: str | None) -> bool:
        floor = self._floors.get(doc_id)
        if floor is None:
            return False
        generation, until = floor
        if self._clock() >= until or (revision is not None and _generation(revision) >= generation):
            del self._floors[doc_id]
            return False
        return True

    def _put(self, note: Note) -> None:
        self._drop(note.id)
        if not isinstance(note.content, str):
            note = replace(note, content=b"")
        self._notes[note.id] = note
        self._keys.setdefault(path_key(note.path), set()).add(note.id)

    def _drop(self, doc_id: str) -> None:
        note = self._notes.pop(doc_id, None)
        if note is not None:
            key = path_key(note.path)
            self._keys[key].discard(doc_id)
            if not self._keys[key]:
                del self._keys[key]


# --- tools ---------------------------------------------------------------------------------

class VaultTools:
    """Tool implementations. Errors are VaultError subclasses whose text starts with their code."""

    def __init__(
        self,
        store: Store,
        catalog: Catalog,
        *,
        catalog_follower: Follower | None = None,
        index_follower: Follower | None = None,
    ):
        self._store = store
        self._catalog = catalog
        self._catalog_follower = catalog_follower
        self._index_follower = index_follower

    # --- reading ---------------------------------------------------------------------------

    def read_note(self, path: str) -> dict:
        target = self._resolve(note_path(path))
        note = self._store.read(target)
        self._require_text(note)
        return {
            "path": note.path, "content": note.content, "revision": note.revision,
            "mtime": note.mtime, "conflicted": note.conflicted,
        }

    def list_notes(self, folder: str = "") -> list[str]:
        prefix = folder_path(folder, allow_root=True)
        return [n.path for n in self._markdown() if not prefix or path_key(n.path).startswith(path_key(prefix) + "/")]

    def search_notes(self, query: str, max_results: int = 10) -> list[dict]:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results = []
        for n in self._markdown():
            m = pattern.search(n.content)
            if m:
                start = max(0, m.start() - 100)
                results.append({"path": n.path, "excerpt": n.content[start:start + 300].strip()})
                if len(results) >= max_results:
                    break
        return results

    def get_backlinks(self, path: str) -> list[str]:
        target = _link_target(note_path(path))[1]
        pattern = re.compile(r"\[\[" + re.escape(target) + r"(\|[^\]]+)?\]\]", re.IGNORECASE)
        return [n.path for n in self._markdown() if _link_target(n.path)[1] != target and pattern.search(n.content)]

    def get_tags(self) -> list[str]:
        tag = re.compile(r"(?<![`\w])#([a-zA-Z][a-zA-Z0-9/_-]*)")
        tags: set[str] = set()
        for n in self._markdown():
            text = re.sub(r"```.*?```", "", n.content, flags=re.DOTALL)
            tags.update(tag.findall(re.sub(r"`[^`]*`", "", text)))
        return sorted(tags)

    def vault_status(self) -> dict:
        try:
            g = self._store.guard.status(force=True)
            guard = {"writable": g.writable, "readable": g.readable, "reason": g.reason}
        except StoreUnavailable as e:
            guard = {"writable": False, "readable": False, "reason": str(e)}
        return {
            **guard,
            "catalog_loaded": self._catalog.loaded,
            "notes": sum(1 for _ in self._markdown(require_loaded=False)),
            "catalog": self._progress(self._catalog_follower),
            "semantic_index": self._progress(self._index_follower),
        }

    # --- writing ------------------------------------------------------------------------

    def write_note(self, path: str, content: str, expected_revision: str | None = None) -> dict:
        target = self._resolve(note_path(path))
        note = self._store.put(target, content, expected_revision)
        self._catalog.wrote(note)
        if expected_revision is None:
            status = "created"
        else:
            status = "unchanged" if note.revision == expected_revision else "updated"
        return {"path": note.path, "revision": note.revision, "status": status}

    def append_to_note(self, path: str, text: str) -> dict:
        return self._edit(self._resolve(note_path(path)), lambda content: content + text, create=text)

    def replace_in_note(self, path: str, old: str, new: str) -> dict:
        target = self._resolve(note_path(path))

        def replace_once(content: str) -> str:
            if not old:
                raise AmbiguousMatch("`old` is empty, which matches everywhere; give the exact text to replace")
            count = content.count(old)
            if count == 0:
                raise NoMatch(f"the text to replace does not occur in {target!r}")
            if count > 1:
                raise AmbiguousMatch(
                    f"the text to replace occurs {count} times in {target!r}; include more surrounding text "
                    f"so it occurs exactly once"
                )
            return content.replace(old, new, 1)

        return self._edit(target, replace_once)

    def delete_note(self, path: str, expected_revision: str) -> dict:
        target = self._resolve(note_path(path))
        revision = self._store.delete(target, expected_revision)
        self._catalog.deleted(fmt.path2id(target), revision)
        return {"path": target, "revision": revision, "status": "deleted"}

    def move_note(self, src: str, dst: str, expected_revision: str) -> dict:
        return self._move_note(self._existing(note_path(src)), note_path(dst), expected_revision)

    def rename_note(self, path: str, new_name: str, expected_revision: str) -> dict:
        source = self._existing(note_path(path))
        return self._move_note(source, _join(_parent(source), note_path(name_segment(new_name))), expected_revision)

    def move_folder(self, src: str, dst: str) -> dict:
        return self._move_folder(folder_path(src), folder_path(dst))

    def rename_folder(self, path: str, new_name: str) -> dict:
        source = folder_path(path)
        return self._move_folder(source, _join(_parent(source), name_segment(new_name)))

    # --- internals ------------------------------------------------------------------------

    def _require_loaded(self) -> None:
        if not self._catalog.loaded:
            raise StoreUnavailable("the vault catalog is still loading from CouchDB; retry shortly")

    def _resolve(self, path: str) -> str:
        """The existing note's stored path, or the NFC form of `path` for a new note."""
        self._require_loaded()
        return self._catalog.resolve(path) or _nfc(path)

    def _existing(self, path: str) -> str:
        self._require_loaded()
        found = self._catalog.resolve(path)
        if found is None:
            raise NotFound(f"no note at {path!r}")
        return found

    def _markdown(self, *, require_loaded: bool = True) -> list[Note]:
        if require_loaded:
            self._require_loaded()
        return [n for n in self._catalog.snapshot() if _is_markdown(n)]

    @staticmethod
    def _require_text(note: Note) -> None:
        if not isinstance(note.content, str):
            raise InvalidPath(f"{note.path!r} is an attachment, not a text note")

    @staticmethod
    def _progress(follower: Follower | None) -> dict | None:
        if follower is None:
            return None
        p = follower.progress()
        return {"seq": p.seq, "pending": p.pending, "rebuilding": p.rebuilding}

    def _edit(self, target: str, change: Callable[[str], str], *, create: str | None = None) -> dict:
        """Read, change, write conditionally; on CONFLICT re-read and retry, up to MAX_ATTEMPTS."""
        last: VaultError | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                note = self._store.read(target)
            except NotFound:
                if create is None:
                    raise
                try:
                    written = self._store.put(target, create)
                except Exists as e:  # created concurrently: append to that
                    last = e
                    continue
                self._catalog.wrote(written)
                return {"path": written.path, "revision": written.revision, "status": "created"}
            self._require_text(note)
            try:
                written = self._store.put(note.path, change(note.content), expected_revision=note.revision)
            except Conflict as e:
                last = e
                continue
            self._catalog.wrote(written)
            status = "unchanged" if written.revision == note.revision else "updated"
            return {"path": written.path, "revision": written.revision, "status": status}
        raise Conflict(
            f"{target!r} kept changing while being edited; gave up after {MAX_ATTEMPTS} attempts. Retry later.",
            getattr(last, "current_revision", None),
        )

    def _relocate(self, source: str, target: str, expected_revision: str | None) -> tuple[str, list[str]]:
        """Move one document (note or attachment) to `target`; returns its final path and the steps done.

        The destination is acknowledged before the source is deleted, so no
        step removes content that exists nowhere else.
        """
        if fmt.path2id(target) == fmt.path2id(source):  # letter case only: same document
            if expected_revision is None:
                current = self._store.raw_doc(source)
                if current is None or fmt.is_deleted(current):
                    raise NotFound(f"no note at {source!r}")
                expected_revision = current["_rev"]
            note = self._store.set_path(source, target, expected_revision)
            self._catalog.wrote(note)
            return note.path, ["changed letter case"]
        existing = self._catalog.resolve(target)
        if existing is not None and fmt.path2id(existing) != fmt.path2id(source):
            raise Exists.for_path(existing)
        target = existing or _nfc(target)
        copied, source_revision = self._store.copy(source, target, expected_revision)
        self._catalog.wrote(copied)
        try:
            revision = self._store.delete(source, source_revision)
        except Conflict as e:
            raise Conflict(
                f"{source!r} changed during the move. The destination {target!r} was created (revision "
                f"{copied.revision}) but the source was kept, so no content is lost. Read both, then delete the "
                f"one you do not want.",
                e.current_revision,
            ) from e
        self._catalog.deleted(fmt.path2id(source), revision)
        return copied.path, ["created destination", "deleted source"]

    def _move_note(self, source: str, target: str, expected_revision: str) -> dict:
        final, steps = self._relocate(source, target, expected_revision)
        rewritten, skipped = self._rewrite_links([(source, final)])
        steps.append(f"rewrote links in {len(rewritten)} note{'' if len(rewritten) == 1 else 's'}")
        revision = self._catalog.get(fmt.path2id(final))
        return {
            "source": source, "destination": final, "revision": revision.revision if revision else None,
            "steps": steps, "links_rewritten": rewritten, "links_skipped": skipped,
        }

    def _move_folder(self, source: str, target: str) -> dict:
        prefix = path_key(source) + "/"
        members = [n for n in self._catalog_snapshot() if path_key(n.path).startswith(prefix)]
        if not members:
            raise NotFound(f"no notes or attachments under folder {source!r}")
        depth = source.count("/") + 1
        documents, moves = [], []
        for n in members:
            destination = _join(_nfc(target), "/".join(n.path.split("/")[depth:]))
            try:
                final, _ = self._relocate(n.path, destination, None)
            except VaultError as e:
                documents.append({"from": n.path, "to": destination, "status": "failed", "error": str(e)})
                continue
            documents.append({"from": n.path, "to": final, "status": "moved"})
            moves.append((n.path, final))
        rewritten, skipped = self._rewrite_links(moves)
        return {
            "moved": len(moves), "failed": len(documents) - len(moves), "documents": documents,
            "links_rewritten": rewritten, "links_skipped": skipped,
        }

    def _catalog_snapshot(self) -> list[Note]:
        self._require_loaded()
        return self._catalog.snapshot()

    def _rewrite_links(self, moves: list[tuple[str, str]]) -> tuple[list[str], list[dict]]:
        """Rewrite links to moved documents in every note, the moved ones included."""
        rules = _link_rules(moves)
        if not rules:
            return [], []
        rewritten, skipped = [], []
        for n in self._markdown():
            if _apply_rules(rules, n.content) == n.content:
                continue
            try:
                result = self._edit(n.path, lambda content: _apply_rules(rules, content))
            except VaultError as e:
                skipped.append({"path": n.path, "error": str(e)})
                continue
            if result["status"] == "updated":
                rewritten.append(result["path"])
        return rewritten, skipped
