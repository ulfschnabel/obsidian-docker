"""Store operations against a real (throwaway) CouchDB."""
import unicodedata
from urllib.parse import quote

import httpx
import pytest

from vaultstore import format as fmt
from vaultstore.errors import Conflict, Exists, IncompatibleVault, IncompleteNote, InvalidPath, NotFound, StoreUnavailable
from vaultstore.store import Store
from vaultstore.testing import init_livesync_db

pytestmark = pytest.mark.couchdb


class Clock:
    def __init__(self, ms: int = 1_790_000_000_000):
        self.ms = ms

    def __call__(self) -> int:
        self.ms += 1000
        return self.ms


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(couch_db, clock):
    return Store(couch_db.client(), clock=clock)


def raw(db, path):
    with db.client() as c:
        r = c.get("/" + quote(fmt.path2id(path), safe=""))
        return r.json() if r.status_code == 200 else None


# --- create and read -------------------------------------------------------

def test_create_then_read(store, couch_db):
    rev = store.write("Inbox/Idea.md", "# Idea\n\nBody.\n")
    note = store.read("Inbox/Idea.md")
    assert note.content == "# Idea\n\nBody.\n"
    assert note.revision == rev
    assert note.path == "Inbox/Idea.md"
    assert not note.conflicted
    doc = raw(couch_db, "Inbox/Idea.md")
    assert doc["_id"] == "inbox/idea.md" and doc["type"] == "plain" and doc["eden"] == {}
    assert doc["size"] == fmt.content_size("# Idea\n\nBody.\n")


@pytest.mark.parametrize("content", ["", "CRLF\r\n\r\nlines\r\n", "🎉 émoji 日本語\n", "big paragraph\n\n" * 20_000])
def test_round_trip(store, content):
    store.write("RT/note.md", content)
    assert store.read("RT/note.md").content == content


def test_nfd_path_is_stored_unnormalised(store, couch_db):
    nfd = unicodedata.normalize("NFD", "Café.md")
    store.write(nfd, "x")
    assert raw(couch_db, nfd)["path"] == nfd


def test_read_missing_is_not_found(store):
    with pytest.raises(NotFound):
        store.read("nope.md")


def test_read_incomplete_note(store, couch_db):
    with couch_db.client() as c:
        c.put("/broken.md", json=fmt.note_doc("broken.md", children=["h:doesnotexist"], size=1, ctime=1, mtime=1)).raise_for_status()
    with pytest.raises(IncompleteNote):
        store.read("broken.md")


def test_create_over_live_note_is_exists(store, couch_db):
    rev = store.write("a.md", "one")
    with pytest.raises(Exists):
        store.write("a.md", "two")
    assert store.read("a.md").content == "one" and store.read("a.md").revision == rev


def test_create_over_logically_deleted_note_resurrects(store, couch_db):
    rev = store.write("a.md", "one")
    store.delete("a.md", rev)
    new_rev = store.write("a.md", "two")
    note = store.read("a.md")
    assert note.content == "two" and note.revision == new_rev
    assert "deleted" not in raw(couch_db, "a.md")


def test_create_over_couchdb_tombstone(store, couch_db):
    rev = store.write("a.md", "one")
    with couch_db.client() as c:
        c.delete("/a.md", params={"rev": rev}).raise_for_status()
    store.write("a.md", "two")
    assert store.read("a.md").content == "two"


# --- update ------------------------------------------------------------------

def test_update_with_current_revision(store, couch_db):
    rev1 = store.write("Notes/Case.md", "one")
    before = raw(couch_db, "Notes/Case.md")
    rev2 = store.write("notes/case.md", "two", expected_revision=rev1)  # different casing, same note
    after = raw(couch_db, "Notes/Case.md")
    assert rev2 != rev1 and store.read("Notes/Case.md").content == "two"
    assert after["ctime"] == before["ctime"] and after["mtime"] > before["mtime"]
    assert after["path"] == "Notes/Case.md"  # casing changes only through a move


def test_stale_revision_is_conflict(store):
    rev1 = store.write("a.md", "one")
    rev2 = store.write("a.md", "phone edit", expected_revision=rev1)
    with pytest.raises(Conflict) as e:
        store.write("a.md", "agent edit", expected_revision=rev1)
    assert e.value.current_revision == rev2
    assert store.read("a.md").content == "phone edit"


def test_update_of_missing_note_is_not_found(store):
    with pytest.raises(NotFound):
        store.write("a.md", "x", expected_revision="1-abc")


def test_identical_content_creates_no_revision(store, couch_db):
    rev = store.write("a.md", "same\n\ncontent")
    assert store.write("a.md", "same\n\ncontent", expected_revision=rev) == rev
    assert raw(couch_db, "a.md")["_rev"] == rev


def test_identical_content_is_judged_by_bytes_not_chunking(store, couch_db):
    # A device chunks differently: one chunk holding the whole text.
    text = "para one\n\npara two\n"
    chunk = fmt.chunk_doc(text)
    with couch_db.client() as c:
        c.post("/_bulk_docs", json={"docs": [chunk], "new_edits": False}).raise_for_status()
        rev = c.put("/dev.md", json=fmt.note_doc("dev.md", children=[chunk["_id"]], size=fmt.content_size(text), ctime=1, mtime=1)).json()["rev"]
    assert store.write("dev.md", text, expected_revision=rev) == rev


# --- write protocol ------------------------------------------------------------

def test_chunks_are_written_before_the_note(couch_db, clock):
    rec = couch_db.recording_client()
    Store(rec.client, clock=clock).write("a.md", "one\n\ntwo")
    writes = rec.writes()
    assert writes[0] == ("POST", f"/{couch_db.name}/_bulk_docs")
    assert writes[-1] == ("PUT", f"/{couch_db.name}/a.md")


class FailingBulk(httpx.BaseTransport):
    def __init__(self):
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request):
        if request.url.path.endswith("/_bulk_docs"):
            return httpx.Response(500, json={"error": "simulated"})
        return self.inner.handle_request(request)


def test_chunk_failure_aborts_before_the_note(couch_db, clock):
    client = httpx.Client(base_url=couch_db.url, auth=couch_db.server.auth, transport=FailingBulk())
    with pytest.raises(StoreUnavailable):
        Store(client, clock=clock).write("a.md", "content")
    assert raw(couch_db, "a.md") is None


def test_never_modifies_chunks_or_control_documents(couch_db, clock):
    rec = couch_db.recording_client()
    s = Store(rec.client, clock=clock)
    rev = s.write("a.md", "one\n\ntwo\n\nthree")
    old_children = s.raw_doc("a.md")["children"]
    rev = s.write("a.md", "four", expected_revision=rev)
    s.delete("a.md", rev)
    for method, path in rec.writes():
        assert method in ("POST", "PUT"), (method, path)
        tail = path.split("/", 2)[2]
        assert not tail.startswith(("_local", "_design", "obsydian_livesync_version", "h:")), path
        if method == "POST":
            assert tail == "_bulk_docs"
    with couch_db.client() as c:
        for cid in old_children:
            assert c.get(f"/{cid}").status_code == 200  # old chunks still there


# --- delete -------------------------------------------------------------------

def test_logical_delete(store, couch_db):
    rev = store.write("a.md", "one")
    children = raw(couch_db, "a.md")["children"]
    store.delete("a.md", rev)
    doc = raw(couch_db, "a.md")
    assert doc["deleted"] is True and doc["children"] == children
    with pytest.raises(NotFound):
        store.read("a.md")


def test_delete_stale_is_conflict(store):
    rev1 = store.write("a.md", "one")
    store.write("a.md", "two", expected_revision=rev1)
    with pytest.raises(Conflict):
        store.delete("a.md", rev1)
    assert store.read("a.md").content == "two"


def test_delete_missing_is_not_found(store):
    with pytest.raises(NotFound):
        store.delete("a.md", "1-abc")


# --- refusals --------------------------------------------------------------------

def test_colon_path_writes_nothing(couch_db, clock):
    rec = couch_db.recording_client()
    with pytest.raises(InvalidPath):
        Store(rec.client, clock=clock).write("Colon/a:b.md", "x")
    assert rec.writes() == []


def test_incompatible_vault_writes_nothing(bare_db, clock):
    init_livesync_db(bare_db, tweaks={"encrypt": True})
    rec = bare_db.recording_client()
    with pytest.raises(IncompatibleVault):
        Store(rec.client, clock=clock).write("a.md", "x")
    assert rec.writes() == []


def test_unreachable_couchdb_is_store_unavailable(clock):
    s = Store(httpx.Client(base_url="http://127.0.0.1:9/nodb", timeout=2), clock=clock)
    with pytest.raises(StoreUnavailable):
        s.write("a.md", "x")
    with pytest.raises(StoreUnavailable):
        s.read("a.md")


def test_read_reports_conflicts(store, couch_db):
    rev1 = store.write("a.md", "one")
    doc1 = raw(couch_db, "a.md")
    store.write("a.md", "two", expected_revision=rev1)
    # A device's concurrent edit of rev1 arrives by replication: a second leaf.
    sibling = {**doc1, "_rev": "2-" + "f" * 32, "_revisions": {"start": 2, "ids": ["f" * 32, rev1.split("-")[1]]}}
    with couch_db.client() as c:
        c.post("/_bulk_docs", json={"docs": [sibling], "new_edits": False}).raise_for_status()
    assert store.read("a.md").conflicted
