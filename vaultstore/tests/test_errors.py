"""Error taxonomy: every error carries a stable code that MCP callers can act on."""
import pytest

from vaultstore import errors


@pytest.mark.parametrize(
    "cls, code",
    [
        (errors.Exists, "EXISTS"),
        (errors.Conflict, "CONFLICT"),
        (errors.NotFound, "NOT_FOUND"),
        (errors.IncompleteNote, "INCOMPLETE_NOTE"),
        (errors.IncompatibleVault, "INCOMPATIBLE_VAULT"),
        (errors.StoreUnavailable, "STORE_UNAVAILABLE"),
        (errors.InvalidPath, "INVALID_PATH"),
        (errors.NoMatch, "NO_MATCH"),
        (errors.AmbiguousMatch, "AMBIGUOUS_MATCH"),
    ],
)
def test_codes_are_stable_and_lead_the_message(cls, code):
    e = cls("something happened")
    assert isinstance(e, errors.VaultError)
    assert e.code == code
    assert str(e).startswith(f"{code}: ")


def test_conflict_carries_current_revision_and_tells_caller_to_reread():
    e = errors.Conflict.for_path("Inbox/a.md", current_revision="7-abc")
    assert e.current_revision == "7-abc"
    assert "7-abc" in str(e)
    assert "read" in str(e).lower() and "expected_revision" in str(e)


def test_exists_tells_caller_to_reread():
    e = errors.Exists.for_path("Inbox/a.md")
    assert "Inbox/a.md" in str(e)
    assert "read" in str(e).lower() and "expected_revision" in str(e)


def test_incomplete_note_lists_missing_chunks():
    e = errors.IncompleteNote.for_path("a.md", missing=["h:1", "h:2"])
    assert e.missing == ["h:1", "h:2"]
    assert "a.md" in str(e)
