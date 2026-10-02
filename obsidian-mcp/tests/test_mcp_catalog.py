"""The in-memory catalog the MCP lists, searches and resolves paths with (design D5, D8)."""
import unicodedata

import pytest

from vault_tools import Catalog
from vaultstore import format as fmt
from vaultstore.errors import AmbiguousMatch
from vaultstore.store import Note


def note(path, content="x", rev="1-a", type="plain"):
    return Note(id=fmt.path2id(path), path=path, content=content, revision=rev, ctime=1, mtime=1,
                size=len(content), type=type, conflicted=False)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def catalog(clock):
    return Catalog(clock=clock)


def test_loaded_once_the_initial_rebuild_reconciles(catalog):
    assert not catalog.loaded
    catalog.apply(note("a.md"))
    assert not catalog.loaded
    catalog.reconcile(frozenset({"a.md"}))
    assert catalog.loaded


def test_reconcile_drops_notes_that_are_not_live(catalog):
    catalog.apply(note("a.md"))
    catalog.apply(note("b.md"))
    catalog.reconcile(frozenset({"b.md"}))
    assert [n.path for n in catalog.snapshot()] == ["b.md"]


def test_resolve_exact_case_insensitive_and_unicode_form(catalog):
    nfd = unicodedata.normalize("NFD", "Café/Résumé.md")
    catalog.apply(note("Inbox/Idea.md"))
    catalog.apply(note(nfd))
    assert catalog.resolve("inbox/idea.md") == "Inbox/Idea.md"
    assert catalog.resolve("Café/Résumé.md") == nfd  # NFC request, NFD note
    assert catalog.resolve("Missing.md") is None


def test_resolve_prefers_the_exact_note_over_a_unicode_twin(catalog):
    nfc = "Résumé.md"
    nfd = unicodedata.normalize("NFD", nfc)
    catalog.apply(note(nfc))
    catalog.apply(note(nfd))
    assert catalog.resolve(nfc) == nfc
    assert catalog.resolve(nfd) == nfd
    assert catalog.resolve("RÉSUMÉ.md") == nfc  # LiveSync ids ignore case: still exact
    mixed = "Résumé.md"  # one accent decomposed, one composed: neither note exactly
    with pytest.raises(AmbiguousMatch):
        catalog.resolve(mixed)


def test_moving_a_note_rekeys_it(catalog):
    catalog.apply(note("notes/case.md"))
    catalog.apply(note("Notes/Case.md", rev="2-b"))
    assert catalog.resolve("NOTES/CASE.md") == "Notes/Case.md"
    assert len(catalog.snapshot()) == 1


def test_attachments_are_kept_as_metadata_only(catalog):
    catalog.apply(note("img/p.png", content=b"\x89PNG" * 1000, type="newnote"))
    (n,) = catalog.snapshot()
    assert n.type == "newnote" and n.content == b"" and n.path == "img/p.png"


def test_remove(catalog):
    catalog.apply(note("a.md"))
    catalog.remove("a.md", "2-x")
    catalog.remove("never-seen.md")
    assert catalog.snapshot() == [] and catalog.resolve("a.md") is None


# --- read-your-writes: the MCP's own writes are not undone by older feed events ---------

def test_older_feed_event_does_not_undo_our_write(catalog):
    catalog.apply(note("a.md", "device v2", rev="2-a"))
    catalog.wrote(note("a.md", "agent v3", rev="3-b"))
    catalog.apply(note("a.md", "device v2", rev="2-a"))  # fetched before our write, applied after
    assert catalog.get("a.md").content == "agent v3"
    catalog.apply(note("a.md", "device v4", rev="4-c"))
    assert catalog.get("a.md").content == "device v4"


def test_older_feed_event_does_not_resurrect_our_delete(catalog):
    catalog.apply(note("a.md", rev="2-a"))
    catalog.deleted("a.md", "3-d")
    catalog.apply(note("a.md", rev="2-a"))
    assert catalog.get("a.md") is None


def test_older_feed_removal_does_not_undo_our_write(catalog):
    catalog.wrote(note("a.md", "resurrected", rev="5-r"))
    catalog.remove("a.md", "4-d")
    assert catalog.get("a.md").content == "resurrected"


def test_write_protection_expires(catalog, clock):
    catalog.wrote(note("a.md", "ours", rev="5-r"))
    clock.now += 61
    catalog.apply(note("a.md", "recreated database", rev="1-n"))
    assert catalog.get("a.md").content == "recreated database"
