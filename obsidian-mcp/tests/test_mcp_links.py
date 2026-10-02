"""Wikilink rewriting keeps the pre-CouchDB server's semantics, on vault-relative path strings."""
from vault_tools import rewrite_links


def test_path_qualified_link_with_and_without_alias():
    text = "See [[Projects/Alpha]] and [[projects/alpha|the plan]]."
    assert rewrite_links(text, [("Projects/Alpha.md", "Archive/Alpha.md")]) == \
        "See [[Archive/Alpha]] and [[Archive/Alpha|the plan]]."


def test_stem_link_rewritten_when_the_name_changes():
    text = "[[Alpha]] [[alpha|a]] [[Alphabet]] [[Other/Alpha]]"
    assert rewrite_links(text, [("Projects/Alpha.md", "Projects/Beta.md")]) == \
        "[[Beta]] [[Beta|a]] [[Alphabet]] [[Other/Alpha]]"


def test_stem_link_kept_when_only_the_folder_changes():
    text = "[[Alpha]] [[Projects/Alpha]]"
    assert rewrite_links(text, [("Projects/Alpha.md", "Archive/Alpha.md")]) == "[[Alpha]] [[Archive/Alpha]]"


def test_regex_metacharacters_in_names_are_literal():
    text = "[[Notes/v1.2 (draft)]] [[Notes/v1x2 (draft)]]"
    assert rewrite_links(text, [("Notes/v1.2 (draft).md", "Notes/v1.3.md")]) == "[[Notes/v1.3]] [[Notes/v1x2 (draft)]]"


def test_attachment_links_keep_their_extension():
    text = "![[img/pic.png]] ![[img/pic.png|200]]"
    assert rewrite_links(text, [("img/pic.png", "media/pic.png")]) == "![[media/pic.png]] ![[media/pic.png|200]]"


def test_several_moves_at_once():
    text = "[[A/x]] [[A/y|why]]"
    assert rewrite_links(text, [("A/x.md", "B/x.md"), ("A/y.md", "B/y.md")]) == "[[B/x]] [[B/y|why]]"


def test_no_moves_no_change():
    assert rewrite_links("[[a]]", []) == "[[a]]"
