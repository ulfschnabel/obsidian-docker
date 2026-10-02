"""Read and write notes in the LiveSync CouchDB database.

Writes follow design D3: check the path and the vault, check the caller's
revision, write chunks, then the note, conditional on the revision. A write
has happened only when CouchDB answered 201; anything else raises.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from . import format as fmt
from .errors import Conflict, Exists, IncompleteNote, NotFound, StoreUnavailable
from .guard import Guard


def doc_path(doc_id: str) -> str:
    """URL path for a document id; '/' inside ids must be encoded."""
    return "/" + quote(doc_id, safe="")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Note:
    id: str
    path: str
    content: str | bytes
    revision: str
    ctime: int
    mtime: int
    size: int
    type: str
    conflicted: bool


class Store:
    def __init__(self, client: httpx.Client, *, guard: Guard | None = None, clock: Callable[[], int] = _now_ms):
        self._c = client
        self.guard = guard or Guard(client)
        self._now = clock

    # --- HTTP ------------------------------------------------------------------

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            return self._c.request(method, url, **kw)
        except httpx.TransportError as e:
            raise StoreUnavailable(f"CouchDB unreachable: {e}") from e

    def _get_doc(self, doc_id: str, *, conflicts: bool = False) -> dict | None:
        r = self._request("GET", doc_path(doc_id), params={"conflicts": "true"} if conflicts else None)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise StoreUnavailable(f"GET {doc_id!r} returned HTTP {r.status_code}")
        return r.json()

    def fetch_chunks(self, ids: Iterable[str]) -> dict[str, str]:
        keys = list(dict.fromkeys(ids))
        if not keys:
            return {}
        r = self._request("POST", "/_all_docs", params={"include_docs": "true"}, json={"keys": keys})
        if r.status_code >= 400:
            raise StoreUnavailable(f"chunk read returned HTTP {r.status_code}")
        return {
            row["id"]: row["doc"]["data"]
            for row in r.json()["rows"]
            if row.get("doc") and row["doc"].get("type") == "leaf"
        }

    def put_chunks(self, chunk_docs: Iterable[dict]) -> None:
        """Write chunk documents with their deterministic revisions (idempotent)."""
        docs = list({d["_id"]: d for d in chunk_docs}.values())
        if not docs:
            return
        r = self._request("POST", "/_bulk_docs", json={"docs": docs, "new_edits": False})
        if r.status_code >= 400:
            raise StoreUnavailable(f"chunk write returned HTTP {r.status_code}")
        failed = [x for x in r.json() if "error" in x]
        if failed:
            raise StoreUnavailable(f"chunk write failed for {len(failed)} chunk(s): {failed[:3]}")

    def put_note(self, doc: dict, path: str) -> str:
        """PUT a note document; only HTTP 201 counts as written."""
        r = self._request("PUT", doc_path(doc["_id"]), json=doc)
        if r.status_code == 201:
            return r.json()["rev"]
        if r.status_code == 409:
            current = self._get_doc(doc["_id"])
            if "_rev" not in doc and current is not None and not fmt.is_deleted(current):
                raise Exists.for_path(path)
            raise Conflict.for_path(path, current["_rev"] if current else None)
        raise StoreUnavailable(f"PUT {doc['_id']!r} returned HTTP {r.status_code}")

    # --- reads -----------------------------------------------------------------

    def raw_doc(self, path: str) -> dict | None:
        """The note document as stored (live or deleted), or None."""
        return self._get_doc(fmt.path2id(path))

    def decode(self, doc: dict) -> str | bytes:
        return fmt.decode_content(doc, self.fetch_chunks(doc.get("children", [])))

    def read(self, path: str) -> Note:
        self.guard.ensure_readable()
        doc = self._get_doc(fmt.path2id(path), conflicts=True)
        if doc is None or fmt.is_deleted(doc):
            raise NotFound(f"no note at {path!r}")
        return Note(
            id=doc["_id"],
            path=doc["path"],
            content=self.decode(doc),
            revision=doc["_rev"],
            ctime=doc.get("ctime", 0),
            mtime=doc.get("mtime", 0),
            size=doc.get("size", 0),
            type=doc.get("type", "plain"),
            conflicted=bool(doc.get("_conflicts")),
        )

    # --- writes ----------------------------------------------------------------

    def write(self, path: str, content: str, expected_revision: str | None = None) -> str:
        """Create (no expected_revision) or update a text note; returns the new revision."""
        fmt.check_path(path)
        self.guard.ensure_writable()
        current = self._get_doc(fmt.path2id(path))
        live = current is not None and not fmt.is_deleted(current)
        if expected_revision is None:
            if live:
                raise Exists.for_path(path)
        else:
            if not live:
                raise NotFound(f"no note at {path!r} to update; create it without expected_revision")
            if current["_rev"] != expected_revision:
                raise Conflict.for_path(path, current["_rev"])
            if self._same_content(current, content):
                return current["_rev"]

        pieces = fmt.split_text(content)
        self.put_chunks(fmt.chunk_doc(p) for p in pieces)
        now = self._now()
        doc = fmt.note_doc(
            current["path"] if live else path,  # casing changes only through a move
            children=[fmt.chunk_id(p) for p in pieces],
            size=fmt.content_size(content),
            ctime=current.get("ctime", now) if live else now,
            mtime=now,
            rev=current["_rev"] if current is not None else None,  # resurrects a logically deleted note
        )
        return self.put_note(doc, path)

    def delete(self, path: str, expected_revision: str) -> str:
        """Logically delete a note (children kept); returns the new revision."""
        fmt.check_path(path)
        self.guard.ensure_writable()
        current = self._get_doc(fmt.path2id(path))
        if current is None or fmt.is_deleted(current):
            raise NotFound(f"no note at {path!r}")
        if current["_rev"] != expected_revision:
            raise Conflict.for_path(path, current["_rev"])
        return self.put_note(fmt.mark_deleted(current, mtime=self._now()), path)

    def _same_content(self, doc: dict, content: str) -> bool:
        if doc.get("type", "plain") != "plain":
            return False
        try:
            return self.decode(doc) == content
        except IncompleteNote:
            return False
