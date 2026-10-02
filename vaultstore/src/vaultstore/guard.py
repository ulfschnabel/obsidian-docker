"""Compatibility guard: only write when the vault's settings are ones we reproduce exactly.

LiveSync only runs its own compatibility check for replicating clients; a raw
HTTP writer never meets it. So we check the same remote documents ourselves
and refuse, loudly, rather than write documents devices would misread.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

import httpx

from .errors import IncompatibleVault, StoreUnavailable

MILESTONE = "/_local/obsydian_livesync_milestone"
VERSION_DOC = "/obsydian_livesync_version"
MAX_VERSION = 12
# Settings that change the stored format. All must be false for us to write;
# the first two also make content unreadable.
BLOCKS_READS = ("encrypt", "enableCompression")
BLOCKS_WRITES = BLOCKS_READS + ("usePathObfuscation", "handleFilenameCaseSensitive")


@dataclass(frozen=True)
class GuardStatus:
    writable: bool
    readable: bool
    reason: str | None
    # The milestone's `created`: identifies this incarnation of the database,
    # which a device's "rebuild remote" replaces. None without a milestone.
    incarnation: int | None = None


class Guard:
    def __init__(self, client: httpx.Client, *, ttl_s: float = 60, clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._ttl = ttl_s
        self._clock = clock
        self._lock = Lock()
        self._cached: tuple[float, GuardStatus] | None = None

    def status(self, *, force: bool = False) -> GuardStatus:
        with self._lock:
            now = self._clock()
            if force or self._cached is None or now - self._cached[0] >= self._ttl:
                self._cached = (now, self._evaluate())
            return self._cached[1]

    def ensure_writable(self) -> None:
        s = self.status()
        if not s.writable:
            raise IncompatibleVault(f"refusing to write: {s.reason}")

    def ensure_readable(self) -> None:
        s = self.status()
        if not s.readable:
            raise IncompatibleVault(f"refusing to read: {s.reason}")

    def _get(self, path: str) -> dict | None:
        try:
            r = self._client.get(path)
        except httpx.TransportError as e:
            raise StoreUnavailable(f"CouchDB unreachable: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise StoreUnavailable(f"GET {path} returned HTTP {r.status_code}")
        return r.json()

    def _evaluate(self) -> GuardStatus:
        milestone = self._get(MILESTONE)
        if milestone is None:
            return GuardStatus(False, True, "LiveSync milestone document missing; the database was not initialised by LiveSync")
        created = milestone.get("created")
        incarnation = created if isinstance(created, int) else None
        preferred = (milestone.get("tweak_values") or {}).get("PREFERRED")
        if preferred is None:
            return GuardStatus(False, True, "LiveSync milestone has no PREFERRED tweak values", incarnation)
        enabled = [k for k in BLOCKS_WRITES if preferred.get(k)]
        if enabled:
            readable = not any(k in BLOCKS_READS for k in enabled)
            return GuardStatus(False, readable, f"unsupported vault setting(s) enabled: {', '.join(enabled)}", incarnation)
        version = self._get(VERSION_DOC)
        if version is None:
            return GuardStatus(False, True, "LiveSync version document missing", incarnation)
        if version.get("version", 0) > MAX_VERSION:
            return GuardStatus(
                False, False, f"database version {version.get('version')} is newer than supported ({MAX_VERSION})", incarnation,
            )
        if milestone.get("locked"):
            return GuardStatus(False, True, "the remote database is locked (a device is rebuilding it)", incarnation)
        return GuardStatus(True, True, None, incarnation)
