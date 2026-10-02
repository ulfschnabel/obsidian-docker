"""The semantic index: a Chroma projection of the vault's markdown notes (design D7).

Driven by a Follower with a persisted checkpoint. Entries are keyed by note
id and remember a hash of the text they were embedded from, so a rebuild
only embeds notes whose text changed.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable

from vaultstore.store import Note

COLLECTION = "vault_notes"  # keyed by note id; the pre-CouchDB "vault" collection was keyed by file path
EMBED_CHARS = 8000
EXCERPT_CHARS = 2000


def _indexable(note: Note) -> bool:
    return (
        note.type == "plain" and isinstance(note.content, str)
        and note.path.lower().endswith(".md") and bool(note.content.strip())
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


class SemanticIndex:
    def __init__(self, collection, embed: Callable[[str], list[float]]):
        self._col = collection
        self._embed_fn = embed
        self._embed_lock = threading.Lock()  # the follower thread and tool calls share the model

    def _embed(self, text: str) -> list[float]:
        with self._embed_lock:
            return list(self._embed_fn(text))

    # Projection

    def apply(self, note: Note) -> None:
        if not _indexable(note):
            self.remove(note.id)
            return
        digest = _digest(note.content)
        existing = self._col.get(ids=[note.id], include=["metadatas"])
        if existing["ids"]:
            meta = existing["metadatas"][0] or {}
            if meta.get("sha256") == digest and meta.get("path") == note.path:
                return
        self._col.upsert(
            ids=[note.id],
            embeddings=[self._embed(note.content[:EMBED_CHARS])],
            documents=[note.content[:EXCERPT_CHARS]],
            metadatas=[{"path": note.path, "filename": note.path.rsplit("/", 1)[-1], "sha256": digest}],
        )

    def remove(self, doc_id: str, revision: str | None = None) -> None:
        self._col.delete(ids=[doc_id])

    def reconcile(self, live_ids: frozenset[str]) -> None:
        stale = [i for i in self._col.get(include=[])["ids"] if i not in live_ids]
        if stale:
            self._col.delete(ids=stale)

    # Tool

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        r = self._col.query(query_embeddings=[self._embed(query)], n_results=min(n_results, 10))
        if not r["documents"] or not r["documents"][0]:
            return []
        return [
            {"path": m["path"], "filename": m["filename"], "excerpt": d[:500], "score": round(1 - s, 3)}
            for d, m, s in zip(r["documents"][0], r["metadatas"][0], r["distances"][0])
        ]
