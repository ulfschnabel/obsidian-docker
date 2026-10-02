"""Path validation and resolution keys (design D8)."""
import unicodedata

import pytest

from vault_tools import folder_path, name_segment, note_path, path_key
from vaultstore.errors import InvalidPath


@pytest.mark.parametrize("path", [
    "", "../../etc/passwd", "/etc/passwd", "a/../b.md", "./a.md", "..",
    ".obsidian/plugins/x.md", "a/.hidden/x.md", ".trash/x.md",
    "Notes/a:b.md", "C:/x.md", "a\\b.md", "a*b.md", 'a"b.md', "a<b.md", "a>b.md", "a|b.md", "a?b.md",
    "a\x00b.md", "a\nb.md", "a\tb.md", "a//b.md", "a/",
])
def test_invalid_note_paths(path):
    with pytest.raises(InvalidPath):
        note_path(path)


@pytest.mark.parametrize("given, expected", [
    ("Inbox/idea", "Inbox/idea.md"),
    ("Inbox/idea.md", "Inbox/idea.md"),
    ("Notes/Upper.MD", "Notes/Upper.MD"),
    ("notes/v1.2", "notes/v1.2.md"),
    ("Ümlaut Ördner/Größe ✓", "Ümlaut Ördner/Größe ✓.md"),
    ("a b/c d.md", "a b/c d.md"),
    ("_underscore/x", "_underscore/x.md"),
])
def test_valid_note_paths(given, expected):
    assert note_path(given) == expected


def test_folder_paths():
    assert folder_path("Projects/") == "Projects"
    assert folder_path("Projects/Alpha") == "Projects/Alpha"
    assert folder_path("", allow_root=True) == ""
    for bad in ("", "/abs", "a/../b", ".hidden", "a:b", "a//b"):
        with pytest.raises(InvalidPath):
            folder_path(bad)


def test_name_segment():
    assert name_segment("New Name") == "New Name"
    for bad in ("a/b", "", ".hidden", "a:b"):
        with pytest.raises(InvalidPath):
            name_segment(bad)


def test_path_key_ignores_unicode_form_and_letter_case():
    nfc = "Café/Résumé.md"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert path_key(nfc) == path_key(nfd) == path_key("CAFÉ/résumé.MD")
