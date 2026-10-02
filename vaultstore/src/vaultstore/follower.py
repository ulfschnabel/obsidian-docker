"""Follow CouchDB's _changes feed into a projection (design D5, D7).

One follower feeds one projection: the MCP's catalog and semantic index, or
the file mirror. It only reads. Its progress is a checkpoint, the feed
sequence plus the notes still waiting for chunks. The checkpoint is saved
atomically after each batch and is bound to the database incarnation, so a
recreated database is rebuilt instead of being half-replayed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

import httpx

from . import format as fmt
from .errors import IncompatibleVault, IncompleteNote, StoreUnavailable
from .guard import Guard
from .store import Note, Store, note_from_doc, request

log = logging.getLogger(__name__)

# Note documents and tombstones only, so the feed never carries chunk bodies.
# Tombstones ({_id, _rev, _deleted}) match the `_deleted` clause (verified).
NOTE_SELECTOR = {"selector": {"$or": [{"type": {"$in": ["plain", "newnote"]}}, {"_deleted": True}]}}
# Not vault notes, even when typed like one: chunks, hidden-file sync,
# customisation sync, design documents, and the version document.
_NON_NOTE_PREFIXES = ("h:", "i:", "ix:", "ps:", "_design/")
_NON_NOTE_IDS = frozenset({"obsydian_livesync_version"})

T = TypeVar("T")


def is_note_id(doc_id: str) -> bool:
    return doc_id not in _NON_NOTE_IDS and not doc_id.startswith(_NON_NOTE_PREFIXES)


class Projection(Protocol):
    """A one-way view of the vault. Every method must be idempotent."""

    def apply(self, note: Note) -> None:
        """Create or replace the note."""

    def remove(self, doc_id: str) -> None:
        """Forget the note; an unknown id is a no-op."""

    def reconcile(self, live_ids: frozenset[str]) -> None:
        """After a full rebuild: forget every note not in `live_ids`."""


@dataclass(frozen=True)
class Checkpoint:
    incarnation: int  # the milestone's `created` of the database the sequence belongs to
    seq: str
    pending: frozenset[str] = frozenset()  # notes seen before all their chunks


class CheckpointFile:
    """A checkpoint persisted as JSON and replaced atomically."""

    VERSION = 1

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def load(self) -> Checkpoint | None:
        """The saved checkpoint, or None when it is missing or invalid (both mean: rebuild)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            d = json.loads(raw)
            if not isinstance(d, dict) or d.get("version") != self.VERSION:
                raise ValueError("not a version-1 checkpoint")
            incarnation, seq, pending = d["incarnation"], d["seq"], d["pending"]
            if (
                not isinstance(incarnation, int) or isinstance(incarnation, bool)
                or not isinstance(seq, str)
                or not isinstance(pending, list) or not all(isinstance(p, str) for p in pending)
            ):
                raise ValueError("malformed fields")
            return Checkpoint(incarnation, seq, frozenset(pending))
        except (ValueError, KeyError) as e:
            log.warning("ignoring invalid checkpoint %s (%s); the projection will be rebuilt", self.path, e)
            return None

    def save(self, cp: Checkpoint) -> None:
        data = json.dumps(
            {"version": self.VERSION, "incarnation": cp.incarnation, "seq": cp.seq, "pending": sorted(cp.pending)},
            ensure_ascii=False,
        ).encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class FollowerFailed(Exception):
    """CouchDB stayed unavailable beyond the retry budget; the process should exit non-zero."""


class Follower:
    """Keeps a projection current with the database.

    Without a checkpoint store (the MCP's in-memory catalog), every start is
    a full load. Transient CouchDB failures are retried with backoff for up to
    `retry_budget_s`, then FollowerFailed is raised. IncompatibleVault (a vault
    it cannot read) and projection errors are raised immediately.
    """

    def __init__(
        self,
        client: httpx.Client,
        projection: Projection,
        checkpoints: CheckpointFile | None = None,
        *,
        batch_size: int = 100,
        longpoll_timeout_s: float = 30,
        retry_budget_s: float = 300,
        backoff_s: tuple[float, float] = (1, 30),
        pending_backoff_s: tuple[float, float] = (1, 300),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._c = client
        self._projection = projection
        self._checkpoints = checkpoints
        self._chunks = Store(client)  # used only to fetch chunks
        self._guard = Guard(client, ttl_s=0, clock=clock)
        self._batch = batch_size
        self._longpoll = longpoll_timeout_s
        self._budget = retry_budget_s
        self._backoff = backoff_s
        self._pending_backoff = pending_backoff_s
        self._clock = clock
        self._sleep = sleep

        self._incarnation: int | None = None
        self._seq = "0"
        self._pending: dict[str, tuple[float, float]] = {}  # id -> (next attempt, last delay)
        self._rebuilding = False
        self._live: set[str] = set()  # during a rebuild: ids of live notes seen so far
        self._saved: Checkpoint | None = None

    # --- public ------------------------------------------------------------------

    def catch_up(self) -> None:
        """Apply everything the feed holds now, plus pending notes that are due."""
        while not self._retrying(lambda: self._step(wait_s=None)):
            pass

    def run(self, stop: threading.Event) -> None:
        """Catch up, then follow the live feed until `stop` is set."""
        self.catch_up()
        while not stop.is_set():
            self._retrying(lambda: self._step(wait_s=self._wait_s()))

    # --- one step: a batch of the feed --------------------------------------------

    def _step(self, *, wait_s: float | None) -> bool:
        """Process one batch; True once the feed is drained."""
        self._check_incarnation()
        body = self._changes(None if self._rebuilding else wait_s)
        self._apply_rows(body["results"])
        self._seq = body["last_seq"]
        drained = body.get("pending", 0) == 0
        self._retry_due()
        if self._rebuilding:
            if not drained:
                return False  # no checkpoint until the rebuild has reconciled
            self._projection.reconcile(frozenset(self._live | self._pending.keys()))
            self._rebuilding, self._live = False, set()
            log.info("rebuild complete at %s", self._seq)
        self._save()
        return drained

    def _check_incarnation(self) -> None:
        status = self._guard.status(force=True)
        if not status.readable:
            raise IncompatibleVault(f"refusing to read: {status.reason}")
        if status.incarnation is None:
            raise StoreUnavailable("the LiveSync milestone is missing; waiting for the database to be initialised")
        if status.incarnation == self._incarnation:
            return
        starting = self._incarnation is None
        cp = self._checkpoints.load() if starting and self._checkpoints else None
        if cp is not None and cp.incarnation == status.incarnation:
            now = self._clock()
            self._seq, self._rebuilding, self._saved = cp.seq, False, cp
            self._pending = {doc_id: (now, 0.0) for doc_id in cp.pending}  # retry at once
        else:
            if not starting:
                log.warning("the database was recreated (milestone created %s, was %s); rebuilding",
                            status.incarnation, self._incarnation)
            elif cp is not None:
                log.warning("the checkpoint belongs to an earlier incarnation of the database; rebuilding")
            else:
                log.info("no checkpoint; rebuilding")
            self._seq, self._pending, self._live, self._rebuilding = "0", {}, set(), True
        self._incarnation = status.incarnation

    def _changes(self, wait_s: float | None) -> dict:
        params = {
            "filter": "_selector", "include_docs": "true", "conflicts": "true",
            "since": self._seq, "limit": str(self._batch),
        }
        kw = {}
        if wait_s and wait_s >= 0.001:
            params.update(feed="longpoll", timeout=str(int(wait_s * 1000)))
            kw["timeout"] = httpx.Timeout(30.0, read=wait_s + 30)
        r = request(self._c, "POST", "/_changes", params=params, json=NOTE_SELECTOR, **kw)
        if r.status_code >= 400:
            raise StoreUnavailable(f"_changes returned HTTP {r.status_code}")
        return r.json()

    def _apply_rows(self, rows: list[dict]) -> None:
        docs = []
        for row in rows:  # each id appears once per response, at its latest change
            doc_id = row["id"]
            if not is_note_id(doc_id):
                continue
            doc = row.get("doc")
            if row.get("deleted") or doc is None or fmt.is_deleted(doc):
                self._remove(doc_id)
            else:
                docs.append(doc)
        self._apply_docs(docs)

    def _apply_docs(self, docs: list[dict]) -> None:
        if not docs:
            return
        chunks = self._chunks.fetch_chunks(c for d in docs for c in d.get("children", []))
        for doc in docs:
            try:
                note = note_from_doc(doc, fmt.decode_content(doc, chunks))
            except IncompleteNote as e:
                self._defer(doc["_id"], f"{len(e.missing)} chunk(s) not replicated yet")
                continue
            except (ValueError, KeyError) as e:  # malformed by its writer; a later revision may fix it
                self._defer(doc["_id"], f"cannot decode: {e!r}")
                continue
            self._projection.apply(note)
            self._pending.pop(doc["_id"], None)
            if self._rebuilding:
                self._live.add(doc["_id"])

    def _remove(self, doc_id: str) -> None:
        self._projection.remove(doc_id)
        self._pending.pop(doc_id, None)
        self._live.discard(doc_id)

    # --- pending notes ----------------------------------------------------------------

    def _defer(self, doc_id: str, reason: str) -> None:
        lo, hi = self._pending_backoff
        _, last = self._pending.get(doc_id, (0.0, 0.0))
        delay = lo if last <= 0 else min(last * 2, hi)
        self._pending[doc_id] = (self._clock() + delay, delay)
        if self._rebuilding:
            self._live.add(doc_id)  # live, so a reconcile keeps what the projection has for it
        log.info("note %s not applied (%s); retrying in %gs", doc_id, reason, delay)

    def _retry_due(self) -> None:
        now = self._clock()
        due = [doc_id for doc_id, (at, _) in self._pending.items() if at <= now]
        if not due:
            return
        r = request(self._c, "POST", "/_all_docs", params={"include_docs": "true", "conflicts": "true"},
                    json={"keys": due})
        if r.status_code >= 400:
            raise StoreUnavailable(f"pending note read returned HTTP {r.status_code}")
        docs = []
        for row in r.json()["rows"]:
            doc = row.get("doc")
            if doc is None or fmt.is_deleted(doc):  # gone, tombstoned, or logically deleted
                self._remove(row["key"])
            else:
                docs.append(doc)
        self._apply_docs(docs)

    def _wait_s(self) -> float:
        if not self._pending:
            return self._longpoll
        soonest = min(at for at, _ in self._pending.values()) - self._clock()
        return max(0.0, min(self._longpoll, soonest))

    # --- checkpoint and retries ------------------------------------------------------

    def _save(self) -> None:
        if self._checkpoints is None:
            return
        cp = Checkpoint(self._incarnation, self._seq, frozenset(self._pending))
        if cp != self._saved:
            self._checkpoints.save(cp)
            self._saved = cp

    def _retrying(self, fn: Callable[[], T]) -> T:
        delay, failing_since = self._backoff[0], None
        while True:
            try:
                return fn()
            except StoreUnavailable as e:
                now = self._clock()
                if failing_since is None:
                    failing_since = now
                if now - failing_since >= self._budget:
                    raise FollowerFailed(f"CouchDB unavailable for {now - failing_since:.0f}s: {e}") from e
                log.warning("%s; retrying in %gs", e, delay)
                self._sleep(delay)
                delay = min(delay * 2, self._backoff[1])
