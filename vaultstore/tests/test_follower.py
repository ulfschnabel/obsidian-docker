"""The _changes follower that drives every projection (design D5, D7)."""
import base64
import os
import threading
import time
from urllib.parse import quote

import httpx
import pytest

from vaultstore import format as fmt
from vaultstore.errors import IncompatibleVault
from vaultstore.follower import Checkpoint, CheckpointFile, Follower, FollowerFailed
from vaultstore.store import Store
from vaultstore.testing import init_livesync_db

pytestmark = pytest.mark.couchdb


class Clock:
    """Monotonic clock whose sleep advances time instead of waiting."""

    def __init__(self, on_sleep=None):
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """A projection that keeps applied notes in memory and logs every call."""

    def __init__(self):
        self.notes = {}
        self.calls = []
        self.removed_revs = {}

    def apply(self, note):
        self.notes[note.id] = note
        self.calls.append(("apply", note.id, note.revision))

    def remove(self, doc_id, revision=None):
        self.notes.pop(doc_id, None)
        self.calls.append(("remove", doc_id))
        self.removed_revs[doc_id] = revision

    def reconcile(self, live_ids):
        self.calls.append(("reconcile", set(live_ids)))
        for doc_id in [i for i in self.notes if i not in live_ids]:
            del self.notes[doc_id]

    def contents(self):
        return {i: n.content for i, n in self.notes.items()}

    def reconciles(self):
        return [c for c in self.calls if c[0] == "reconcile"]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cp_path(tmp_path):
    return tmp_path / "state" / "checkpoint.json"


def make(db_or_client, projection, checkpoints, clock, **kw):
    client = db_or_client if isinstance(db_or_client, httpx.Client) else db_or_client.client()
    kw = {"retry_budget_s": 60, "backoff_s": (1, 8), "pending_backoff_s": (1, 300), **kw}
    return Follower(client, projection, checkpoints, clock=clock, sleep=clock.sleep, **kw)


def put_raw(db, doc) -> str:
    with db.client() as c:
        r = c.put("/" + quote(doc["_id"], safe=""), json=doc)
        r.raise_for_status()
        return r.json()["rev"]


def put_chunks(db, docs) -> None:
    with db.client() as c:
        c.post("/_bulk_docs", json={"docs": list(docs), "new_edits": False}).raise_for_status()


def put_attachment(db, path, pieces: list[bytes]) -> None:
    """An attachment as a device writes it: each piece Base64-encoded separately."""
    chunks = [fmt.chunk_doc(base64.b64encode(p).decode()) for p in pieces]
    put_chunks(db, chunks)
    put_raw(db, fmt.note_doc(path, children=[c["_id"] for c in chunks], size=sum(map(len, pieces)),
                             ctime=1, mtime=1, type="newnote"))


def recreate(db, **init) -> None:
    """What a device's "rebuild remote" does: drop the database and initialise a new one."""
    with db.server.client() as c:
        c.delete(f"/{db.name}").raise_for_status()
        c.put(f"/{db.name}").raise_for_status()
    init_livesync_db(db, **init)


# --- initial load and following -------------------------------------------------

def test_initial_load_applies_live_notes_and_reconciles(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    s.write("A.md", "alpha\n\nmore")
    s.delete("B.md", s.write("B.md", "beta"))
    s.write("C/Ü.md", "über")
    put_attachment(couch_db, "img/p.png", [b"\x89PNG\r\n\x1a\n", b"\x00\x01binary"])
    for doc_id in ("i:hidden.md", "ix:x", "ps:x"):  # hidden-file and plugin sync, not vault notes
        put_raw(couch_db, {"_id": doc_id, "path": doc_id, "type": "plain", "children": [], "size": 0})

    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()

    assert rec.contents() == {"a.md": "alpha\n\nmore", "c/ü.md": "über", "img/p.png": b"\x89PNG\r\n\x1a\n\x00\x01binary"}
    assert rec.notes["c/ü.md"].path == "C/Ü.md"
    assert rec.reconciles() == [("reconcile", {"a.md", "c/ü.md", "img/p.png"})]
    assert not [c for c in rec.calls if c[0] != "reconcile" and c[1].startswith(("i:", "ix:", "ps:", "h:", "_"))]
    cp = CheckpointFile(cp_path).load()
    assert cp is not None and cp.pending == frozenset()


def test_follows_later_changes(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    rev_a = s.write("A.md", "one")
    rev_b = s.write("B.md", "bee")
    rec = Recorder()
    f = make(couch_db, rec, CheckpointFile(cp_path), clock)
    f.catch_up()

    rev_a = s.write("A.md", "two", expected_revision=rev_a)
    s.delete("B.md", rev_b)
    s.write("D.md", "dee")
    f.catch_up()

    assert rec.contents() == {"a.md": "two", "d.md": "dee"}
    assert rec.notes["a.md"].revision == rev_a
    assert len(rec.reconciles()) == 1  # only the initial rebuild


def test_couchdb_tombstone_removes_the_note(couch_db, cp_path, clock):
    rev = Store(couch_db.client()).write("A.md", "one")
    rec = Recorder()
    f = make(couch_db, rec, CheckpointFile(cp_path), clock)
    f.catch_up()
    with couch_db.client() as c:
        c.delete("/a.md", params={"rev": rev}).raise_for_status()
    f.catch_up()
    assert rec.notes == {} and ("remove", "a.md") in rec.calls


def test_removal_carries_the_deleting_revision(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    rev = s.write("A.md", "one")
    rec = Recorder()
    f = make(couch_db, rec, CheckpointFile(cp_path), clock)
    f.catch_up()
    deleting = s.delete("A.md", rev)
    f.catch_up()
    assert rec.removed_revs == {"a.md": deleting}


def test_progress_reports_position_and_pending(couch_db, cp_path, clock):
    late_note(couch_db)
    f = make(couch_db, Recorder(), CheckpointFile(cp_path), clock)
    assert f.progress().seq is None
    f.catch_up()
    p = f.progress()
    assert p.seq == CheckpointFile(cp_path).load().seq and p.pending == 1 and not p.rebuilding


def test_conflicted_note_applies_the_winning_revision(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    rev1 = s.write("A.md", "one")
    with couch_db.client() as c:
        doc1 = c.get("/a.md").json()
    rev2 = s.write("A.md", "two", expected_revision=rev1)
    # A device's concurrent edit of rev1, losing to rev2 (lower revision hash at the same depth).
    loser = {**doc1, "_rev": "2-" + "0" * 32, "_revisions": {"start": 2, "ids": ["0" * 32, rev1.split("-")[1]]}}
    put_chunks(couch_db, [loser])
    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()
    note = rec.notes["a.md"]
    assert note.content == "two" and note.revision == rev2 and note.conflicted


def test_run_follows_the_live_feed_until_stopped(couch_db, cp_path):
    s = Store(couch_db.client())
    s.write("A.md", "one")
    rec = Recorder()
    f = Follower(couch_db.client(), rec, CheckpointFile(cp_path), longpoll_timeout_s=0.5)
    stop, errors = threading.Event(), []

    def target():
        try:
            f.run(stop)
        except Exception as e:  # surfaced below
            errors.append(e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: "a.md" in rec.notes)
        s.write("B.md", "bee")
        assert wait_for(lambda: "b.md" in rec.notes)
    finally:
        stop.set()
        t.join(10)
    assert not t.is_alive() and not errors


def wait_for(predicate, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --- notes whose chunks have not arrived -----------------------------------------

def late_note(db, path="Late.md", text="late content"):
    """A note document that replicated before its chunk."""
    chunk = fmt.chunk_doc(text)
    put_raw(db, fmt.note_doc(path, children=[chunk["_id"]], size=len(text), ctime=1, mtime=1))
    return chunk


def test_note_with_missing_chunks_is_pending_and_does_not_block(couch_db, cp_path, clock):
    chunk = late_note(couch_db)
    Store(couch_db.client()).write("Other.md", "other")
    rec = Recorder()
    f = make(couch_db, rec, CheckpointFile(cp_path), clock)
    f.catch_up()

    assert set(rec.notes) == {"other.md"}
    assert CheckpointFile(cp_path).load().pending == {"late.md"}
    # A pending note is live: a rebuild keeps whatever the projection already had for it.
    assert rec.reconciles() == [("reconcile", {"other.md", "late.md"})]

    put_chunks(couch_db, [chunk])
    f.catch_up()
    assert "late.md" not in rec.notes  # backoff not yet elapsed
    clock.advance(1)
    f.catch_up()
    assert rec.contents()["late.md"] == "late content"
    assert CheckpointFile(cp_path).load().pending == frozenset()


def test_pending_retry_backs_off(couch_db, cp_path, clock):
    late_note(couch_db)
    rc = couch_db.recording_client()
    rec = Recorder()
    f = make(rc.client, rec, CheckpointFile(cp_path), clock, pending_backoff_s=(1, 4))
    f.catch_up()  # at t=1000; the note is deferred
    retried_at = []
    for _ in range(22):
        clock.advance(0.5)
        before = len(rc.requests)
        f.catch_up()
        if any(m == "POST" and p.endswith("/_all_docs") for m, p in rc.requests[before:]):
            retried_at.append(clock.now)
    assert retried_at == [1001, 1003, 1007, 1011]  # 1 s doubling, capped at 4 s
    assert "late.md" not in rec.notes


def test_pending_set_survives_restart(couch_db, cp_path, clock):
    chunk = late_note(couch_db)
    make(couch_db, Recorder(), CheckpointFile(cp_path), clock).catch_up()
    put_chunks(couch_db, [chunk])

    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()  # the feed has nothing new for it
    assert rec.contents() == {"late.md": "late content"}
    assert rec.reconciles() == []


def test_pending_note_deleted_before_its_chunks_arrive(couch_db, cp_path, clock):
    late_note(couch_db)
    rec = Recorder()
    f = make(couch_db, rec, CheckpointFile(cp_path), clock)
    f.catch_up()
    with couch_db.client() as c:
        doc = c.get("/late.md").json()
    put_raw(couch_db, fmt.mark_deleted(doc, mtime=2))
    f.catch_up()
    assert ("remove", "late.md") in rec.calls
    assert CheckpointFile(cp_path).load().pending == frozenset()


def test_undecodable_note_is_pending_not_fatal(couch_db, cp_path, clock):
    bad = fmt.chunk_doc("!!! not base64 !!!")
    put_chunks(couch_db, [bad])
    put_raw(couch_db, fmt.note_doc("x.png", children=[bad["_id"]], size=3, ctime=1, mtime=1, type="newnote"))
    Store(couch_db.client()).write("Fine.md", "fine")
    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()
    assert set(rec.notes) == {"fine.md"}
    assert CheckpointFile(cp_path).load().pending == {"x.png"}


# --- checkpoints ---------------------------------------------------------------------

def test_restart_resumes_after_the_checkpoint(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    rev_a = s.write("A.md", "one")
    make(couch_db, Recorder(), CheckpointFile(cp_path), clock).catch_up()
    rev_b = s.write("B.md", "bee")
    rev_a = s.write("A.md", "two", expected_revision=rev_a)

    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()
    assert sorted(rec.calls) == sorted([("apply", "a.md", rev_a), ("apply", "b.md", rev_b)])


class FailingSave(CheckpointFile):
    def __init__(self, path, fail_on: int):
        super().__init__(path)
        self.fail_on, self.saves = fail_on, 0

    def save(self, cp):
        self.saves += 1
        if self.saves == self.fail_on:
            raise OSError("simulated crash before the checkpoint was written")
        super().save(cp)


def test_crash_before_checkpoint_reapplies_idempotently(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    s.write("A.md", "one")
    rec = Recorder()  # stands in for a persisted projection that outlives the process
    make(couch_db, rec, FailingSave(cp_path, fail_on=2), clock).catch_up()
    rev_b = s.write("B.md", "bee")
    with pytest.raises(OSError):
        make(couch_db, rec, FailingSave(cp_path, fail_on=1), clock).catch_up()
    assert "b.md" in rec.notes  # applied, but the checkpoint did not advance

    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()
    assert rec.calls.count(("apply", "b.md", rev_b)) == 2
    assert rec.contents() == {"a.md": "one", "b.md": "bee"}


def test_checkpoint_round_trip(tmp_path):
    f = CheckpointFile(tmp_path / "deep" / "cp.json")
    assert f.load() is None
    cp = Checkpoint(incarnation=1777986784536, seq="12-g1AAAA", pending=frozenset({"ü.md", "a.md"}))
    f.save(cp)
    assert f.load() == cp


def test_checkpoint_save_is_atomic(tmp_path, monkeypatch):
    f = CheckpointFile(tmp_path / "cp.json")
    f.save(Checkpoint(1, "5-x", frozenset({"a.md"})))

    def crash(*args):
        raise OSError("crash during rename")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        f.save(Checkpoint(1, "9-y", frozenset()))
    monkeypatch.undo()
    assert f.load() == Checkpoint(1, "5-x", frozenset({"a.md"}))
    assert [p.name for p in tmp_path.iterdir()] == ["cp.json"]


@pytest.mark.parametrize("content", [
    b"", b"{not json", b"[]", b'{"version": 99, "incarnation": 1, "seq": "1", "pending": []}',
    b'{"version": 1, "incarnation": "x", "seq": "1", "pending": []}',
    b'{"version": 1, "incarnation": 1, "pending": []}',
    b'{"version": 1, "incarnation": 1, "seq": "1", "pending": "a.md"}',
])
def test_invalid_checkpoint_loads_as_none(tmp_path, content):
    path = tmp_path / "cp.json"
    path.write_bytes(content)
    assert CheckpointFile(path).load() is None


def test_invalid_checkpoint_rebuilds(couch_db, cp_path, clock):
    Store(couch_db.client()).write("A.md", "one")
    cp_path.parent.mkdir(parents=True)
    cp_path.write_bytes(b"{not json")
    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()
    assert rec.reconciles() == [("reconcile", {"a.md"})]
    assert CheckpointFile(cp_path).load() is not None


def test_recreated_database_rebuilds_on_restart(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    s.write("A.md", "one")
    s.write("B.md", "two")
    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()

    recreate(couch_db, created=42)
    Store(couch_db.client()).write("C.md", "three")
    make(couch_db, rec, CheckpointFile(cp_path), clock).catch_up()
    assert rec.contents() == {"c.md": "three"}
    assert CheckpointFile(cp_path).load().incarnation == 42


def test_recreated_database_rebuilds_while_following(couch_db, cp_path, clock):
    Store(couch_db.client()).write("A.md", "one")
    rec = Recorder()
    f = make(couch_db, rec, CheckpointFile(cp_path), clock)
    f.catch_up()

    recreate(couch_db, created=42)
    Store(couch_db.client()).write("C.md", "three")
    f.catch_up()
    assert rec.contents() == {"c.md": "three"}


def test_no_checkpoint_until_a_rebuild_has_reconciled(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    for i in range(5):
        s.write(f"n{i}.md", f"note {i}")

    class Crashing(Recorder):
        def reconcile(self, live_ids):
            raise RuntimeError("crash during reconcile")

    with pytest.raises(RuntimeError):
        make(couch_db, Crashing(), CheckpointFile(cp_path), clock, batch_size=2).catch_up()
    assert CheckpointFile(cp_path).load() is None

    rec = Recorder()
    make(couch_db, rec, CheckpointFile(cp_path), clock, batch_size=2).catch_up()
    assert rec.reconciles() == [("reconcile", {f"n{i}.md" for i in range(5)})]


# --- failures --------------------------------------------------------------------------

def test_unreachable_couchdb_fails_after_the_retry_budget(cp_path, clock):
    client = httpx.Client(base_url="http://127.0.0.1:9/nodb", timeout=2)
    f = make(client, Recorder(), CheckpointFile(cp_path), clock)
    start = clock.now
    with pytest.raises(FollowerFailed):
        f.catch_up()
    assert clock.sleeps[:5] == [1, 2, 4, 8, 8]
    assert 60 <= clock.now - start < 60 + 8


class Flaky(httpx.BaseTransport):
    def __init__(self, failures: int):
        self.inner, self.failures = httpx.HTTPTransport(), failures

    def handle_request(self, request):
        if self.failures:
            self.failures -= 1
            raise httpx.ConnectError("simulated outage", request=request)
        return self.inner.handle_request(request)


def test_transient_outage_is_retried(couch_db, cp_path, clock):
    Store(couch_db.client()).write("A.md", "one")
    client = httpx.Client(base_url=couch_db.url, auth=couch_db.server.auth, transport=Flaky(3))
    rec = Recorder()
    make(client, rec, CheckpointFile(cp_path), clock).catch_up()
    assert set(rec.notes) == {"a.md"} and clock.sleeps == [1, 2, 4]


def test_database_being_initialised_is_waited_for(bare_db, cp_path):
    clock = Clock(on_sleep=lambda: init_livesync_db(bare_db) if len(clock.sleeps) == 2 else None)
    rec = Recorder()
    make(bare_db, rec, CheckpointFile(cp_path), clock).catch_up()
    assert len(clock.sleeps) == 2 and rec.reconciles() == [("reconcile", set())]


def test_unreadable_vault_fails_without_retrying(bare_db, cp_path, clock):
    init_livesync_db(bare_db, tweaks={"encrypt": True})
    with pytest.raises(IncompatibleVault):
        make(bare_db, Recorder(), CheckpointFile(cp_path), clock).catch_up()
    assert clock.sleeps == []


# --- one-way ---------------------------------------------------------------------------

def test_follower_only_reads(couch_db, cp_path, clock):
    s = Store(couch_db.client())
    s.write("A.md", "one")
    s.delete("B.md", s.write("B.md", "two"))
    chunk = late_note(couch_db)
    rc = couch_db.recording_client()
    f = make(rc.client, Recorder(), CheckpointFile(cp_path), clock)
    f.catch_up()
    put_chunks(couch_db, [chunk])
    clock.advance(1)
    f.catch_up()
    recreate(couch_db, created=7)
    f.catch_up()
    assert rc.requests and rc.writes() == []
