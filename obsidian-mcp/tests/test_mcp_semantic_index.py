"""The semantic index as a projection of the change feed (design D7)."""
import base64
import hashlib
import math
import re
from urllib.parse import quote

import pytest

from semantic_index import SemanticIndex
from vaultstore import format as fmt
from vaultstore.follower import CheckpointFile, Follower
from vaultstore.store import Store

pytestmark = pytest.mark.couchdb


class FakeCollection:
    """The subset of a chromadb Collection the index uses, with chromadb's result shapes (l2 space)."""

    def __init__(self):
        self.rows: dict[str, tuple[list[float], str, dict]] = {}

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, e, d, m in zip(ids, embeddings, documents, metadatas):
            self.rows[i] = (list(e), d, dict(m))

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)

    def get(self, ids=None, include=("metadatas", "documents")):
        keys = [i for i in (ids if ids is not None else list(self.rows)) if i in self.rows]
        out = {"ids": keys}
        if "metadatas" in include:
            out["metadatas"] = [self.rows[i][2] for i in keys]
        return out

    def query(self, query_embeddings, n_results):
        q = query_embeddings[0]
        scored = sorted(
            (sum((a - b) ** 2 for a, b in zip(q, e)), i) for i, (e, _, _) in self.rows.items()
        )[:n_results]
        return {
            "ids": [[i for _, i in scored]],
            "documents": [[self.rows[i][1] for _, i in scored]],
            "metadatas": [[self.rows[i][2] for _, i in scored]],
            "distances": [[d for d, _ in scored]],
        }


class Embedder:
    """A deterministic bag-of-words embedding that counts its calls."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        v = [0.0] * 64
        for word in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


class Setup:
    def __init__(self, db, tmp_path, collection=None):
        self.collection = collection or FakeCollection()
        self.embed = Embedder()
        self.index = SemanticIndex(self.collection, self.embed)
        self.follower = Follower(db.client(), self.index, CheckpointFile(tmp_path / "semantic-index.json"))

    def sync(self):
        self.follower.catch_up()

    def paths(self, query, n=10):
        return [r["path"] for r in self.index.search(query, n)]


@pytest.fixture
def store(couch_db):
    return Store(couch_db.client())


def test_notes_are_indexed_and_searchable(couch_db, tmp_path, store):
    store.write("Fruit/Apples.md", "apples and pears grow in orchards")
    store.write("Boats.md", "sailing boats on the sea")
    s = Setup(couch_db, tmp_path)
    s.sync()
    results = s.index.search("orchards with apples", 5)
    top = results[0]
    assert top["path"] == "Fruit/Apples.md" and top["filename"] == "Apples.md"
    assert top["excerpt"] == "apples and pears grow in orchards"
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)  # 1 - l2 distance


def test_deleted_note_leaves_the_index(couch_db, tmp_path, store):
    rev = store.write("A.md", "apples")
    store.write("B.md", "boats")
    s = Setup(couch_db, tmp_path)
    s.sync()
    store.delete("A.md", rev)
    s.sync()
    assert s.paths("apples") == ["B.md"]


def test_moved_note_is_rekeyed(couch_db, tmp_path, store):
    rev = store.write("Old/A.md", "apples")
    s = Setup(couch_db, tmp_path)
    s.sync()
    store.copy("Old/A.md", "New/A.md", rev)
    store.delete("Old/A.md", rev)
    s.sync()
    assert set(s.collection.rows) == {"new/a.md"} and s.paths("apples") == ["New/A.md"]


def test_changed_note_is_reembedded(couch_db, tmp_path, store):
    rev = store.write("A.md", "apples")
    s = Setup(couch_db, tmp_path)
    s.sync()
    store.write("A.md", "boats now", expected_revision=rev)
    s.sync()
    assert s.embed.calls == ["apples", "boats now"] and s.collection.rows["a.md"][1] == "boats now"


def test_restart_with_a_checkpoint_computes_no_embeddings(couch_db, tmp_path, store):
    store.write("A.md", "apples")
    store.write("B.md", "boats")
    first = Setup(couch_db, tmp_path)
    first.sync()
    restarted = Setup(couch_db, tmp_path, collection=first.collection)
    restarted.sync()
    assert restarted.embed.calls == [] and set(first.collection.rows) == {"a.md", "b.md"}


def test_missing_checkpoint_rebuilds_and_removes_stale_entries(couch_db, tmp_path, store):
    store.write("A.md", "apples")
    first = Setup(couch_db, tmp_path)
    first.sync()
    first.collection.upsert(["ghost.md"], [[0.0] * 64], ["ghost"], [{"path": "Ghost.md", "filename": "Ghost.md"}])
    (tmp_path / "semantic-index.json").unlink()
    store.write("B.md", "boats")

    rebuilt = Setup(couch_db, tmp_path, collection=first.collection)
    rebuilt.sync()
    assert set(first.collection.rows) == {"a.md", "b.md"}
    assert rebuilt.embed.calls == ["boats"]  # unchanged notes are not embedded again


def test_only_nonempty_markdown_is_indexed(couch_db, tmp_path, store):
    store.write("Note.md", "words")
    store.write("Empty.md", "  \n")
    store.write("plain.txt", "not markdown")
    chunk = fmt.chunk_doc(base64.b64encode(b"\x89PNG").decode())
    with couch_db.client() as c:
        c.post("/_bulk_docs", json={"docs": [chunk], "new_edits": False}).raise_for_status()
        c.put("/" + quote("p.png", safe=""), json=fmt.note_doc(
            "p.png", children=[chunk["_id"]], size=4, ctime=1, mtime=1, type="newnote")).raise_for_status()
    s = Setup(couch_db, tmp_path)
    s.sync()
    assert set(s.collection.rows) == {"note.md"}


def test_note_emptied_later_leaves_the_index(couch_db, tmp_path, store):
    rev = store.write("A.md", "apples")
    s = Setup(couch_db, tmp_path)
    s.sync()
    store.write("A.md", "", expected_revision=rev)
    s.sync()
    assert s.collection.rows == {}
