"""MCP tool logic on the document store (design D3, D4, D5, D8)."""
import base64
import unicodedata
from urllib.parse import quote

import httpx
import pytest

from vault_tools import MAX_ATTEMPTS, Catalog, VaultTools
from vaultstore import format as fmt
from vaultstore.errors import (
    AmbiguousMatch, Conflict, Exists, IncompatibleVault, InvalidPath, NoMatch, NotFound, StoreUnavailable,
)
from vaultstore.follower import Follower
from vaultstore.store import Store
from vaultstore.testing import init_livesync_db

pytestmark = pytest.mark.couchdb


class Vault:
    """The MCP's parts wired as server.py wires them, plus a phone writing to the same database."""

    def __init__(self, db, store: Store | None = None):
        self.db = db
        self.store = store or Store(db.client())
        self.catalog = Catalog()
        self.follower = Follower(db.client(), self.catalog)
        self.follower.catch_up()
        self.tools = VaultTools(self.store, self.catalog, catalog_follower=self.follower)
        self.phone = Store(db.client())

    def sync(self) -> None:
        """What the catalog's background follower does continuously."""
        self.follower.catch_up()

    def raw(self, path: str) -> dict | None:
        with self.db.client() as c:
            r = c.get("/" + quote(fmt.path2id(path), safe=""))
            return r.json() if r.status_code == 200 else None


@pytest.fixture
def vault(couch_db):
    return Vault(couch_db)


class Interleaving(Store):
    """A store where another writer gets to act right after each read."""

    def __init__(self, client, hook):
        super().__init__(client)
        self.hook, self.reads = hook, 0

    def read(self, path):
        note = super().read(path)
        self.reads += 1
        self.hook(note)
        return note


def put_attachment(db, path, pieces: list[bytes]) -> None:
    chunks = [fmt.chunk_doc(base64.b64encode(p).decode()) for p in pieces]
    with db.client() as c:
        c.post("/_bulk_docs", json={"docs": chunks, "new_edits": False}).raise_for_status()
        c.put("/" + quote(fmt.path2id(path), safe=""), json=fmt.note_doc(
            path, children=[x["_id"] for x in chunks], size=sum(map(len, pieces)), ctime=1, mtime=1, type="newnote",
        )).raise_for_status()


# --- 6.1 paths ----------------------------------------------------------------------------

def test_invalid_paths_touch_no_storage(couch_db):
    rc = couch_db.recording_client()
    v = Vault(couch_db, store=Store(rc.client))
    for call in (
        lambda: v.tools.read_note("../../etc/passwd"),
        lambda: v.tools.write_note(".obsidian/plugins/x.md", "x"),
        lambda: v.tools.write_note("Notes/a:b.md", "x"),
        lambda: v.tools.move_folder("Trip", "/abs"),
    ):
        with pytest.raises(InvalidPath):
            call()
    assert rc.requests == []


def test_missing_extension_is_appended(vault):
    vault.tools.write_note("Inbox/idea", "x")
    assert vault.raw("Inbox/idea.md")["path"] == "Inbox/idea.md"


def test_nfc_path_reaches_a_note_a_device_named_in_nfd(vault):
    nfd = unicodedata.normalize("NFD", "Café/Résumé.md")
    nfc = unicodedata.normalize("NFC", nfd)
    vault.phone.write(nfd, "from the phone")
    vault.sync()
    r = vault.tools.read_note(nfc)
    assert r["content"] == "from the phone" and r["path"] == nfd
    vault.tools.write_note(nfc, "edited", expected_revision=r["revision"])
    assert vault.phone.read(nfd).content == "edited"
    assert vault.raw(nfc) is None  # no second note


def test_new_notes_are_created_in_nfc(vault):
    nfd = unicodedata.normalize("NFD", "Café.md")
    nfc = unicodedata.normalize("NFC", nfd)
    r = vault.tools.write_note(nfd, "x")
    assert r["path"] == nfc and vault.raw(nfc)["path"] == nfc and vault.raw(nfd) is None


def test_letter_case_resolves_to_the_existing_note(vault):
    vault.phone.write("Inbox/Idea.md", "x")
    vault.sync()
    assert vault.tools.read_note("inbox/idea")["path"] == "Inbox/Idea.md"


# --- 6.2 read and write ---------------------------------------------------------------------

def test_read_returns_the_revision(vault):
    rev = vault.phone.write("A.md", "alpha")
    vault.sync()
    assert vault.tools.read_note("A") == {
        "path": "A.md", "content": "alpha", "revision": rev, "mtime": vault.raw("A.md")["mtime"], "conflicted": False,
    }


def test_read_comes_from_couchdb_not_the_catalog(vault):
    rev = vault.phone.write("A.md", "one")
    vault.sync()
    vault.phone.write("A.md", "two", expected_revision=rev)  # the catalog has not seen this
    assert vault.tools.read_note("A.md")["content"] == "two"


def test_read_deleted_or_missing_is_not_found(vault):
    vault.phone.delete("A.md", vault.phone.write("A.md", "x"))
    vault.sync()
    for path in ("A.md", "Nope.md"):
        with pytest.raises(NotFound):
            vault.tools.read_note(path)


def test_create_update_unchanged(vault):
    r1 = vault.tools.write_note("A.md", "one")
    assert r1["status"] == "created" and vault.raw("A.md")["_rev"] == r1["revision"]
    r2 = vault.tools.write_note("A.md", "two", expected_revision=r1["revision"])
    assert r2["status"] == "updated" and vault.raw("A.md")["_rev"] == r2["revision"]
    assert vault.tools.write_note("A.md", "two", expected_revision=r2["revision"]) == {**r2, "status": "unchanged"}


def test_blind_overwrite_is_exists(vault):
    vault.tools.write_note("A.md", "one")
    with pytest.raises(Exists, match="expected_revision"):
        vault.tools.write_note("A.md", "two")
    assert vault.phone.read("A.md").content == "one"


def test_phone_edit_in_between_is_conflict(vault):
    vault.tools.write_note("A.md", "one")
    r = vault.tools.read_note("A.md")
    vault.phone.write("A.md", "phone edit", expected_revision=r["revision"])
    with pytest.raises(Conflict, match="read_note"):
        vault.tools.write_note("A.md", "agent edit", expected_revision=r["revision"])
    assert vault.phone.read("A.md").content == "phone edit"


def test_couchdb_down_is_store_unavailable(vault):
    down = VaultTools(Store(httpx.Client(base_url="http://127.0.0.1:9/nodb", timeout=2)), vault.catalog)
    with pytest.raises(StoreUnavailable):
        down.write_note("A.md", "x")
    with pytest.raises(StoreUnavailable):
        down.read_note("A.md")
    assert vault.raw("A.md") is None


def test_tools_wait_for_the_catalog(couch_db):
    tools = VaultTools(Store(couch_db.client()), Catalog())
    with pytest.raises(StoreUnavailable, match="loading"):
        tools.list_notes()
    with pytest.raises(StoreUnavailable, match="loading"):
        tools.write_note("A.md", "x")


def test_incompatible_vault_refuses_writes(bare_db):
    init_livesync_db(bare_db, tweaks={"usePathObfuscation": True})
    with pytest.raises(IncompatibleVault):
        Vault(bare_db).tools.write_note("A.md", "x")


# --- 6.3 append and replace -------------------------------------------------------------------

def test_append_creates_a_missing_note(vault):
    r = vault.tools.append_to_note("Log", "first\n")
    assert r["status"] == "created" and r["path"] == "Log.md" and vault.phone.read("Log.md").content == "first\n"


def test_append_is_verbatim(vault):
    vault.tools.write_note("Log.md", "a\n")
    r = vault.tools.append_to_note("Log.md", "b\n")
    assert r["status"] == "updated" and vault.phone.read("Log.md").content == "a\nb\n"


def test_append_keeps_a_concurrent_edit(couch_db):
    phone = Store(couch_db.client())
    phone.write("Log.md", "a\n")
    edited = []

    def phone_edits_once(note):
        if not edited:
            edited.append(phone.write("Log.md", note.content + "phone\n", expected_revision=note.revision))

    Vault(couch_db, store=Interleaving(couch_db.client(), phone_edits_once)).tools.append_to_note("Log.md", "agent\n")
    assert phone.read("Log.md").content == "a\nphone\nagent\n"


def test_append_gives_up_after_five_conflicts(couch_db):
    phone = Store(couch_db.client())
    phone.write("Log.md", "a\n")
    store = Interleaving(
        couch_db.client(), lambda note: phone.write("Log.md", note.content + "x", expected_revision=note.revision),
    )
    with pytest.raises(Conflict):
        Vault(couch_db, store=store).tools.append_to_note("Log.md", "agent\n")
    assert store.reads == MAX_ATTEMPTS == 5
    assert "agent" not in phone.read("Log.md").content


def test_replace_unique_match(vault):
    vault.tools.write_note("A.md", "status: draft\nbody")
    r = vault.tools.replace_in_note("A.md", "draft", "final")
    assert vault.phone.read("A.md").content == "status: final\nbody" and r["revision"] == vault.raw("A.md")["_rev"]


@pytest.mark.parametrize("old, error", [("missing", NoMatch), ("a", AmbiguousMatch), ("", AmbiguousMatch)])
def test_replace_refusals_write_nothing(vault, old, error):
    r = vault.tools.write_note("A.md", "a and a")
    with pytest.raises(error):
        vault.tools.replace_in_note("A.md", old, "b")
    assert vault.raw("A.md")["_rev"] == r["revision"]


def test_replace_keeps_a_concurrent_edit(couch_db):
    phone = Store(couch_db.client())
    phone.write("A.md", "status: draft\n")
    edited = []

    def phone_edits_once(note):
        if not edited:
            edited.append(phone.write("A.md", note.content + "phone\n", expected_revision=note.revision))

    Vault(couch_db, store=Interleaving(couch_db.client(), phone_edits_once)).tools.replace_in_note("A.md", "draft", "final")
    assert phone.read("A.md").content == "status: final\nphone\n"


def test_replace_in_missing_note_is_not_found(vault):
    with pytest.raises(NotFound):
        vault.tools.replace_in_note("A.md", "x", "y")


# --- 6.4 delete ----------------------------------------------------------------------------------

def test_delete_with_revision(vault):
    r = vault.tools.write_note("A.md", "x")
    out = vault.tools.delete_note("A.md", r["revision"])
    assert out["path"] == "A.md" and out["revision"] == vault.raw("A.md")["_rev"]
    assert vault.raw("A.md")["deleted"] is True
    with pytest.raises(NotFound):
        vault.tools.read_note("A.md")
    assert vault.tools.list_notes() == []


def test_delete_stale_is_conflict(vault):
    r = vault.tools.write_note("A.md", "x")
    vault.tools.write_note("A.md", "y", expected_revision=r["revision"])
    with pytest.raises(Conflict):
        vault.tools.delete_note("A.md", r["revision"])
    assert vault.tools.read_note("A.md")["content"] == "y"


def test_delete_missing_is_not_found(vault):
    with pytest.raises(NotFound):
        vault.tools.delete_note("A.md", "1-abc")


# --- 6.5 move and rename a note -------------------------------------------------------------------

def test_move_note_with_backlinks(vault):
    a = vault.tools.write_note("Projects/Alpha.md", "alpha body, self link [[Projects/Alpha]]")
    vault.tools.write_note("B.md", "see [[Alpha]]")
    vault.tools.write_note("C.md", "see [[Projects/Alpha|the plan]]")
    vault.tools.write_note("D.md", "unrelated")
    r = vault.tools.move_note("Projects/Alpha", "Archive/Omega", a["revision"])
    assert vault.tools.read_note("Archive/Omega.md")["content"] == "alpha body, self link [[Archive/Omega]]"
    with pytest.raises(NotFound):
        vault.tools.read_note("Projects/Alpha.md")
    assert vault.phone.read("B.md").content == "see [[Omega]]"
    assert vault.phone.read("C.md").content == "see [[Archive/Omega|the plan]]"
    assert r["source"] == "Projects/Alpha.md" and r["destination"] == "Archive/Omega.md"
    assert sorted(r["links_rewritten"]) == ["Archive/Omega.md", "B.md", "C.md"] and r["links_skipped"] == []
    assert r["steps"] == ["created destination", "deleted source", "rewrote links in 3 notes"]
    assert vault.tools.list_notes() == ["Archive/Omega.md", "B.md", "C.md", "D.md"]


def test_move_creates_the_destination_before_deleting_the_source(couch_db):
    rc = couch_db.recording_client()
    v = Vault(couch_db, store=Store(rc.client))
    a = v.tools.write_note("A.md", "x")
    v.tools.write_note("B.md", "[[A]]")
    del rc.requests[:]
    v.tools.move_note("A.md", "Z.md", a["revision"])
    assert [p for m, p in rc.writes() if m == "PUT"] == [f"/{couch_db.name}/{doc}" for doc in ("z.md", "a.md", "b.md")]


def test_move_onto_a_live_note_is_exists_and_writes_nothing(couch_db):
    rc = couch_db.recording_client()
    v = Vault(couch_db, store=Store(rc.client))
    a = v.tools.write_note("A.md", "x")
    v.tools.write_note("Z.md", "z")
    del rc.requests[:]
    with pytest.raises(Exists):
        v.tools.move_note("A.md", "z", a["revision"])
    assert rc.writes() == []


def test_move_with_a_stale_revision_writes_nothing(couch_db):
    rc = couch_db.recording_client()
    v = Vault(couch_db, store=Store(rc.client))
    a = v.tools.write_note("A.md", "x")
    v.tools.write_note("A.md", "y", expected_revision=a["revision"])
    del rc.requests[:]
    with pytest.raises(Conflict):
        v.tools.move_note("A.md", "Z.md", a["revision"])
    assert rc.writes() == []


def test_move_of_a_missing_note_is_not_found(vault):
    with pytest.raises(NotFound):
        vault.tools.move_note("A.md", "Z.md", "1-abc")


def test_source_changed_during_the_move_loses_nothing(couch_db):
    phone = Store(couch_db.client())

    class PhoneEditsAfterCopy(Store):
        def copy(self, src, dst, expected_revision=None):
            result = super().copy(src, dst, expected_revision)
            phone.write(src, "phone edit", expected_revision=result[1])
            return result

    v = Vault(couch_db, store=PhoneEditsAfterCopy(couch_db.client()))
    a = v.tools.write_note("A.md", "original")
    with pytest.raises(Conflict, match="destination 'Z.md' was created") as e:
        v.tools.move_note("A.md", "Z.md", a["revision"])
    assert "kept" in str(e.value)
    assert phone.read("A.md").content == "phone edit" and phone.read("Z.md").content == "original"


def test_rename_note(vault):
    a = vault.tools.write_note("Projects/Alpha.md", "x")
    vault.tools.write_note("B.md", "[[Alpha]]")
    r = vault.tools.rename_note("projects/alpha", "Beta", a["revision"])
    assert r["destination"] == "Projects/Beta.md" and vault.phone.read("B.md").content == "[[Beta]]"


def test_rename_note_letter_case_only(vault):
    a = vault.tools.write_note("notes/case.md", "body")
    r = vault.tools.rename_note("notes/case.md", "Case", a["revision"])
    assert r["destination"] == "notes/Case.md" and r["steps"][0] == "changed letter case"
    assert vault.raw("notes/case.md")["path"] == "notes/Case.md"
    assert vault.tools.list_notes() == ["notes/Case.md"]


def test_rename_note_takes_a_name_not_a_path(vault):
    a = vault.tools.write_note("A.md", "x")
    with pytest.raises(InvalidPath):
        vault.tools.rename_note("A.md", "elsewhere/B", a["revision"])


def test_links_that_keep_conflicting_are_reported_and_the_move_completes(couch_db):
    phone = Store(couch_db.client())

    def phone_keeps_editing_b(note):
        if note.path == "B.md":
            phone.write("B.md", note.content + " x", expected_revision=note.revision)

    v = Vault(couch_db, store=Interleaving(couch_db.client(), phone_keeps_editing_b))
    a = v.tools.write_note("A.md", "x")
    v.tools.write_note("B.md", "[[A]]")
    r = v.tools.move_note("A.md", "Z.md", a["revision"])
    assert [s["path"] for s in r["links_skipped"]] == ["B.md"] and "CONFLICT" in r["links_skipped"][0]["error"]
    assert v.phone.read("Z.md").content == "x"
    with pytest.raises(NotFound):
        v.tools.read_note("A.md")


# --- 6.6 move and rename a folder ---------------------------------------------------------------

def test_move_folder_with_an_attachment(vault):
    vault.tools.write_note("Trip/Plan.md", "map: ![[Trip/map.png]]")
    put_attachment(vault.db, "Trip/map.png", [b"\x89PNG", b"\x00data"])
    vault.sync()
    vault.tools.write_note("Index.md", "[[Trip/Plan]]")
    r = vault.tools.move_folder("Trip", "Archive/Trip")
    assert vault.phone.read("Archive/Trip/map.png").content == b"\x89PNG\x00data"
    assert vault.phone.read("Archive/Trip/Plan.md").content == "map: ![[Archive/Trip/map.png]]"
    assert vault.phone.read("Index.md").content == "[[Archive/Trip/Plan]]"
    for path in ("Trip/Plan.md", "Trip/map.png"):
        assert vault.raw(path)["deleted"] is True
    assert r["moved"] == 2 and r["failed"] == 0
    assert sorted(d["to"] for d in r["documents"]) == ["Archive/Trip/Plan.md", "Archive/Trip/map.png"]
    assert vault.tools.list_notes() == ["Archive/Trip/Plan.md", "Index.md"]


def test_move_folder_reports_per_document_outcomes(vault):
    vault.tools.write_note("Trip/a.md", "a")
    vault.tools.write_note("Trip/b.md", "b")
    vault.tools.write_note("Archive/Trip/b.md", "already here")
    r = vault.tools.move_folder("Trip", "Archive/Trip")
    outcome = {d["from"]: d["status"] for d in r["documents"]}
    assert outcome == {"Trip/a.md": "moved", "Trip/b.md": "failed"}
    assert "EXISTS" in next(d["error"] for d in r["documents"] if d["from"] == "Trip/b.md")
    assert vault.phone.read("Trip/b.md").content == "b" and vault.phone.read("Archive/Trip/b.md").content == "already here"


def test_rename_folder(vault):
    vault.tools.write_note("Projects/Alpha/x.md", "x")
    vault.tools.rename_folder("Projects/Alpha", "Beta")
    assert vault.tools.list_notes() == ["Projects/Beta/x.md"]


def test_folder_letter_case_rename(vault):
    vault.tools.write_note("trip/a.md", "a")
    vault.tools.move_folder("trip", "Trip")
    assert vault.raw("trip/a.md")["path"] == "Trip/a.md" and vault.tools.list_notes() == ["Trip/a.md"]


def test_folder_match_is_by_whole_segment(vault):
    vault.tools.write_note("Trip/a.md", "a")
    vault.tools.write_note("Trips/b.md", "b")
    vault.tools.move_folder("trip", "X")
    assert vault.tools.list_notes() == ["Trips/b.md", "X/a.md"]


def test_move_missing_folder_is_not_found(vault):
    with pytest.raises(NotFound):
        vault.tools.move_folder("Nope", "X")


# --- 6.7 listing and search over the catalog -----------------------------------------------------

def test_list_notes(vault):
    for path in ("B.md", "a/C.md", "a/b/D.md"):
        vault.tools.write_note(path, "x")
    vault.phone.delete("E.md", vault.phone.write("E.md", "x"))
    vault.phone.write("a/plain.txt", "not markdown")
    put_attachment(vault.db, "a/p.png", [b"x"])
    vault.sync()
    assert vault.tools.list_notes() == ["B.md", "a/C.md", "a/b/D.md"]
    assert vault.tools.list_notes("A") == ["a/C.md", "a/b/D.md"]
    assert vault.tools.list_notes("a/b/") == ["a/b/D.md"]


def test_search_excerpt_semantics(vault):
    text = "x" * 150 + "Needle" + "y" * 400
    vault.tools.write_note("A.md", text)
    vault.tools.write_note("B.md", "no match")
    assert vault.tools.search_notes("needle") == [{"path": "A.md", "excerpt": text[50:350].strip()}]


def test_search_max_results(vault):
    for name in ("a", "b", "c"):
        vault.tools.write_note(f"{name}.md", "word")
    assert len(vault.tools.search_notes("word", max_results=2)) == 2


def test_read_your_writes_in_search_and_list(vault):
    r = vault.tools.write_note("Fresh.md", "zebracorn")
    assert [x["path"] for x in vault.tools.search_notes("zebracorn")] == ["Fresh.md"]
    vault.tools.delete_note("Fresh.md", r["revision"])
    assert vault.tools.search_notes("zebracorn") == [] and vault.tools.list_notes() == []


def test_device_edit_becomes_visible_without_restart(vault):
    vault.phone.write("Phone/Note.md", "written on the phone: quokka")
    vault.sync()
    assert vault.tools.list_notes() == ["Phone/Note.md"]
    assert vault.tools.search_notes("quokka")[0]["path"] == "Phone/Note.md"


def test_backlinks(vault):
    vault.tools.write_note("Projects/Target.md", "the target")
    vault.tools.write_note("A.md", "[[Target]]")
    vault.tools.write_note("B.md", "[[target|alias]]")
    vault.tools.write_note("C.md", "[[Projects/Target]]")  # not matched, as before
    vault.tools.write_note("D.md", "[[Targets]]")
    vault.tools.write_note("Other/Target.md", "[[Target]]")  # same name as the target
    assert vault.tools.get_backlinks("Projects/Target") == ["A.md", "B.md"]


def test_tags(vault):
    vault.tools.write_note("A.md", "#alpha #beta/x-y `#code` a#notag #1bad\n```\n#block\n```\n")
    vault.tools.write_note("B.md", "#alpha #gamma_1")
    assert vault.tools.get_tags() == ["alpha", "beta/x-y", "gamma_1"]


# --- 6.8 vault_status -------------------------------------------------------------------------------

def test_vault_status(vault):
    chunk = fmt.chunk_doc("late")
    with vault.db.client() as c:
        c.put("/late.md", json=fmt.note_doc("Late.md", children=[chunk["_id"]], size=4, ctime=1, mtime=1)).raise_for_status()
    vault.tools.write_note("A.md", "x")
    vault.sync()
    s = vault.tools.vault_status()
    assert s["writable"] and s["readable"] and s["reason"] is None
    assert s["catalog_loaded"] and s["notes"] == 1
    assert s["catalog"]["seq"] and s["catalog"]["pending"] == 1 and s["semantic_index"] is None


def test_vault_status_reports_the_guard_refusal(bare_db):
    init_livesync_db(bare_db, tweaks={"usePathObfuscation": True})
    s = Vault(bare_db).tools.vault_status()
    assert s["writable"] is False and "usePathObfuscation" in s["reason"] and s["readable"]


def test_vault_status_when_couchdb_is_down(vault):
    down = VaultTools(Store(httpx.Client(base_url="http://127.0.0.1:9/nodb", timeout=2)), vault.catalog)
    s = down.vault_status()
    assert s["writable"] is False and "STORE_UNAVAILABLE" in s["reason"] and s["catalog_loaded"]
