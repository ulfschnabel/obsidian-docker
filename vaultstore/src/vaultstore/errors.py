"""Error taxonomy. Every error message starts with a stable code callers can act on."""
from __future__ import annotations


class VaultError(Exception):
    code = "VAULT_ERROR"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")
        self.message = message


class Exists(VaultError):
    code = "EXISTS"

    @classmethod
    def for_path(cls, path: str) -> Exists:
        return cls(
            f"a note already exists at {path!r}. Call read_note first and pass its revision "
            f"as expected_revision to overwrite it."
        )


class Conflict(VaultError):
    code = "CONFLICT"

    def __init__(self, message: str, current_revision: str | None = None):
        super().__init__(message)
        self.current_revision = current_revision

    @classmethod
    def for_path(cls, path: str, current_revision: str | None) -> Conflict:
        return cls(
            f"{path!r} changed since it was read (current revision {current_revision}). "
            f"Call read_note again and retry with expected_revision set to the new revision.",
            current_revision=current_revision,
        )


class NotFound(VaultError):
    code = "NOT_FOUND"


class IncompleteNote(VaultError):
    code = "INCOMPLETE_NOTE"

    def __init__(self, message: str, missing: list[str] | None = None):
        super().__init__(message)
        self.missing = list(missing or [])

    @classmethod
    def for_path(cls, path: str, missing: list[str]) -> IncompleteNote:
        return cls(
            f"{path!r} references {len(missing)} chunk(s) not yet in the database; "
            f"it may still be replicating from a device. Retry shortly.",
            missing=missing,
        )


class IncompatibleVault(VaultError):
    code = "INCOMPATIBLE_VAULT"


class StoreUnavailable(VaultError):
    code = "STORE_UNAVAILABLE"


class InvalidPath(VaultError):
    code = "INVALID_PATH"


class NoMatch(VaultError):
    code = "NO_MATCH"


class AmbiguousMatch(VaultError):
    code = "AMBIGUOUS_MATCH"
