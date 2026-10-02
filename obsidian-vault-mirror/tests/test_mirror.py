"""The read-only file mirror of the vault (design D7, D9)."""
import base64
import os
import unicodedata
from pathlib import Path
from urllib.parse import quote

import pytest

from mirror import Mirror, main
from vaultstore import format as fmt
from vaultstore.follower import CheckpointFile, Follower
from vaultstore.store import Store
from vaultstore.testing import init_livesync_db

pytestmark = pytest.mark.couchdb


class Clock:
    def __init__(self, ms=1_790_000_000_000):
        self.ms = ms

    def __call__(self):
        self.ms += 1000
        return self.ms


@pytest.fixture
def root(tmp_path):
    return tmp_path / "mirror"


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state" / "mirror.json"


@pytest.fixture
def store(couch_db):
    return Store(couch_db.client(), clock=Clock())


def follow(db, root, state, client=None):
    """Start (or restart) the mirror and bring it up to date."""
    f = Follower(client or db.client(), Mirror(root), CheckpointFile(state))
    f.catch_up()
    return f


def files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def dirs(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()}


def put_raw(db, doc):
    with db.client() as c:
        c.put("/" + quote(doc["_id"], safe=""), json=doc).raise_for_status()


def put_attachment(db, path, pieces, mtime=1_700_000_000_000):
    chunks = [fmt.chunk_doc(base64.b64encode(p).decode()) for p in pieces]
    with db.client() as c:
        c.post("/_bulk_docs", json={"docs": chunks, "new_edits": False}).raise_for_status()
    put_raw(db, fmt.note_doc(path, children=[x["_id"] for x in chunks], size=sum(map(len, pieces)),
                             ctime=1, mtime=mtime, type="newnote"))


# --- 8.1 file semantics -------------------------------------------------------------------

def test_live_notes_are_written_with_their_mtime(couch_db, root, state, store):
    nfd = unicodedata.normalize("NFD", "Café/Résumé.md")
    store.write("Inbox/Idea.md", "# Idea\r\n\r\nGröße ✓\n")
    store.write(nfd, "unicode path")
    store.delete("Gone.md", store.write("Gone.md", "x"))
    follow(couch_db, root, state)
    assert files(root) == {"Inbox/Idea.md": "# Idea\r\n\r\nGröße ✓\n".encode(), nfd: b"unicode path"}
    doc = store.raw_doc("Inbox/Idea.md")
    assert (root / "Inbox/Idea.md").stat().st_mtime_ns == doc["mtime"] * 1_000_000


def test_attachment_is_written_as_its_decoded_bytes(couch_db, root, state):
    put_attachment(couch_db, "img/p.png", [b"\x89PNG\r\n\x1a\n", b"\x00\xffbinary"], mtime=1_700_000_123_000)
    follow(couch_db, root, state)
    assert files(root) == {"img/p.png": b"\x89PNG\r\n\x1a\n\x00\xffbinary"}
    assert (root / "img/p.png").stat().st_mtime_ns == 1_700_000_123_000 * 1_000_000


def test_update_replaces_the_file_atomically(couch_db, root, state, store, monkeypatch):
    rev = store.write("A.md", "old content")
    f = follow(couch_db, root, state)
    store.write("A.md", "new content", expected_revision=rev)
    real_replace, seen = os.replace, []

    def checking_replace(src, dst):
        # Until the rename, readers of the final path see the complete old file.
        if Path(dst) == root / "A.md":
            seen.append((Path(dst).read_bytes(), Path(src).read_bytes()))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", checking_replace)
    f.catch_up()
    assert seen == [(b"old content", b"new content")]
    assert files(root) == {"A.md": b"new content"}


def test_failed_write_leaves_the_old_file_and_no_temporary_file(couch_db, root, state, store, monkeypatch):
    rev = store.write("A.md", "old content")
    f = follow(couch_db, root, state)
    store.write("A.md", "new content", expected_revision=rev)

    def crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        f.catch_up()
    assert files(root) == {"A.md": b"old content"}


def test_deletion_removes_the_file_and_emptied_folders(couch_db, root, state, store):
    rev = store.write("A/B/c.md", "c")
    store.write("A/kept.md", "kept")
    f = follow(couch_db, root, state)
    store.delete("A/B/c.md", rev)
    f.catch_up()
    assert files(root) == {"A/kept.md": b"kept"} and dirs(root) == {"A"}


def test_case_only_rename_removes_the_old_file(couch_db, root, state, store):
    rev = store.write("notes/case.md", "body")
    f = follow(couch_db, root, state)
    store.set_path("notes/case.md", "Notes/Case.md", rev)
    f.catch_up()
    assert files(root) == {"Notes/Case.md": b"body"} and dirs(root) == {"Notes"}


def test_move_writes_the_new_file_and_removes_the_old(couch_db, root, state, store):
    rev = store.write("Old/A.md", "a")
    f = follow(couch_db, root, state)
    store.copy("Old/A.md", "New/A.md", rev)
    store.delete("Old/A.md", rev)
    f.catch_up()
    assert files(root) == {"New/A.md": b"a"} and dirs(root) == {"New"}


def test_deletion_after_a_restart(couch_db, root, state, store):
    rev = store.write("A/a.md", "a")
    follow(couch_db, root, state)
    store.delete("A/a.md", rev)
    follow(couch_db, root, state)  # a new process: it knows the file only from the disk
    assert files(root) == {} and dirs(root) == set()


def test_unchanged_note_does_not_touch_the_file(couch_db, root, state, store):
    store.write("A.md", "a")
    follow(couch_db, root, state)
    before = (root / "A.md").stat()
    state.unlink()  # a rebuild re-applies every note
    follow(couch_db, root, state)
    after = (root / "A.md").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize("bad", ["../escape.md", "/abs.md", "a/../../x.md", "a//b.md", "./a.md", "a\x00b.md"])
def test_unsafe_paths_are_never_written(couch_db, root, state, store, bad, tmp_path):
    chunk = fmt.chunk_doc("evil")
    with couch_db.client() as c:
        c.post("/_bulk_docs", json={"docs": [chunk], "new_edits": False}).raise_for_status()
    doc = fmt.note_doc("placeholder.md", children=[chunk["_id"]], size=4, ctime=1, mtime=1)
    put_raw(couch_db, {**doc, "_id": "evil", "path": bad})
    store.write("Fine.md", "fine")
    follow(couch_db, root, state)
    assert files(root) == {"Fine.md": b"fine"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mirror", "state"]


# --- 8.2 rebuild, reconcile, one-way ----------------------------------------------------------

def test_rebuild_removes_extraneous_files(couch_db, root, state, store):
    store.write("Keep/a.md", "a")
    store.write("notes/Case.md", "current casing")
    (root / "Stray").mkdir(parents=True)
    (root / "Stray/old.md").write_bytes(b"stray")
    (root / "top.md").write_bytes(b"stray")
    (root / "Empty/Deeper").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "notes/case.md").write_bytes(b"stale casing")  # same note id, old casing
    follow(couch_db, root, state)
    assert files(root) == {"Keep/a.md": b"a", "notes/Case.md": b"current casing"}
    assert dirs(root) == {"Keep", "notes"}


def test_leftover_temporary_files_are_removed(couch_db, root, state, store):
    store.write("A.md", "a")
    root.mkdir()
    (root / ".mirror-tmp-0123456789ab").write_bytes(b"half written")
    follow(couch_db, root, state)
    assert files(root) == {"A.md": b"a"}


def test_mirror_never_writes_upstream(couch_db, root, state, store):
    rc = couch_db.recording_client()
    rev = store.write("A.md", "a")
    store.write("B.md", "b")
    follow(couch_db, root, state, client=rc.client)
    store.write("A.md", "a2", expected_revision=rev)
    store.delete("B.md", store.raw_doc("B.md")["_rev"])
    state.unlink()
    follow(couch_db, root, state, client=rc.client)
    assert rc.requests and rc.writes() == []


def env(db, root, tmp_path, **extra):
    return {
        "COUCHDB_URL": db.url, "COUCHDB_USER": db.server.auth[0], "COUCHDB_PASSWORD": db.server.auth[1],
        "MIRROR_ROOT": str(root), "STATE_DIR": str(tmp_path / "state"), **extra,
    }


def test_main_exits_non_zero_when_couchdb_stays_down(couch_db, root, tmp_path):
    e = env(couch_db, root, tmp_path, COUCHDB_URL="http://127.0.0.1:9/nodb", RETRY_BUDGET_S="0")
    assert main(e) == 1


def test_main_exits_non_zero_on_an_unreadable_vault(bare_db, root, tmp_path):
    init_livesync_db(bare_db, tweaks={"encrypt": True})
    assert main(env(bare_db, root, tmp_path)) == 1


def test_main_requires_its_settings(root, tmp_path):
    assert main({"MIRROR_ROOT": str(root)}) == 2
