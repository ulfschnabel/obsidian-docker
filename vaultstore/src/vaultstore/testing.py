"""Pytest plugin shared by the vaultstore, obsidian-mcp and mirror test suites.

Provides a throwaway CouchDB (a `couchdb:3` container, or an existing server
via VAULTSTORE_TEST_COUCHDB_URL), fresh LiveSync-initialised databases per
test, and an httpx client that records every request for one-way assertions.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest

ADMIN_USER = "admin"  # the couchdb image's entrypoint rejects 1-character admin names
ADMIN_PASSWORD = "testpass"

# Copied from the production milestone's tweak_values.PREFERRED (2026-09-27).
PREFERRED_TWEAKS = {
    "useIgnoreFiles": False,
    "useCustomRequestHandler": False,
    "batch_size": 25,
    "batches_limit": 25,
    "useTimeouts": False,
    "readChunksOnline": True,
    "hashCacheMaxCount": 300,
    "hashCacheMaxAmount": 50,
    "concurrencyOfReadChunksOnline": 40,
    "minimumIntervalOfReadChunksOnline": 50,
    "ignoreFiles": ".gitignore",
    "syncMaxSizeInMB": 50,
    "enableChunkSplitterV2": False,
    "usePluginSyncV2": False,
    "doNotUseFixedRevisionForChunks": True,
    "E2EEAlgorithm": "v2",
    "chunkSplitterVersion": "v3-rabin-karp",
    "minimumChunkSize": 20,
    "longLineThreshold": 250,
    "encrypt": False,
    "usePathObfuscation": False,
    "enableCompression": False,
    "useEden": False,
    "customChunkSize": 0,
    "useDynamicIterationCount": False,
    "hashAlg": "xxhash64",
    "maxChunksInEden": 10,
    "maxTotalLengthInEden": 1024,
    "maxAgeInEden": 10,
    "useSegmenter": False,
}


@dataclass
class RecordedClient:
    """An httpx.Client plus the (method, path) of every request it sent."""

    client: httpx.Client
    requests: list[tuple[str, str]] = field(default_factory=list)

    def writes(self) -> list[tuple[str, str]]:
        """Requests that can modify the database (anything but reads)."""
        read_posts = ("/_changes", "/_all_docs", "/_bulk_get")
        return [
            (m, p)
            for m, p in self.requests
            if m not in ("GET", "HEAD") and not (m == "POST" and p.endswith(read_posts))
        ]


@dataclass
class CouchServer:
    url: str  # base URL without credentials, e.g. http://127.0.0.1:49153
    auth: tuple[str, str] = (ADMIN_USER, ADMIN_PASSWORD)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, auth=self.auth, timeout=30)


@dataclass
class TestDb:
    server: CouchServer
    name: str

    @property
    def url(self) -> str:
        return f"{self.server.url}/{self.name}"

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, auth=self.server.auth, timeout=30)

    def recording_client(self) -> RecordedClient:
        rec = RecordedClient(client=None)  # type: ignore[arg-type]

        def log(request: httpx.Request) -> None:
            rec.requests.append((request.method, request.url.path))

        rec.client = httpx.Client(
            base_url=self.url, auth=self.server.auth, timeout=30, event_hooks={"request": [log]}
        )
        return rec


def init_livesync_db(db: TestDb, *, tweaks: dict | None = None, locked: bool = False, version: int = 12) -> None:
    """Create the control documents a LiveSync-initialised remote database carries."""
    with db.client() as c:
        c.put(
            "/_local/obsydian_livesync_milestone",
            json={
                "type": "milestoneinfo",
                "created": int(time.time() * 1000),
                "locked": locked,
                "accepted_nodes": [],
                "node_info": {},
                "node_chunk_info": {},
                "tweak_values": {"PREFERRED": {**PREFERRED_TWEAKS, **(tweaks or {})}},
            },
        ).raise_for_status()
        c.put("/obsydian_livesync_version", json={"version": version, "type": "versioninfo"}).raise_for_status()


def _wait_ready(url: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError as e:
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"CouchDB at {url} not ready after {timeout}s: {last}")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "couchdb: needs a CouchDB (throwaway docker container)")
    config.addinivalue_line("markers", "conformance: needs docker and the livesync-cli image (real LiveSync core)")
    config.addinivalue_line("markers", "export: needs VAULT_EXPORT, a production _all_docs export (never committed)")


@pytest.fixture(scope="session")
def couch_server() -> Iterator[CouchServer]:
    existing = os.environ.get("VAULTSTORE_TEST_COUCHDB_URL")
    if existing:
        server = CouchServer(url=existing.rstrip("/"))
        _wait_ready(server.url)
        yield server
        return
    if not shutil.which("docker"):
        pytest.skip("docker not available and VAULTSTORE_TEST_COUCHDB_URL not set")
    name = f"vaultstore-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", "127.0.0.1::5984",
            "-e", f"COUCHDB_USER={ADMIN_USER}", "-e", f"COUCHDB_PASSWORD={ADMIN_PASSWORD}",
            "couchdb:3",
        ],
        check=True, capture_output=True,
    )
    try:
        port = subprocess.run(
            ["docker", "port", name, "5984/tcp"], check=True, capture_output=True, text=True
        ).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        server = CouchServer(url=f"http://127.0.0.1:{port}")
        _wait_ready(server.url)
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def couch_db(couch_server: CouchServer) -> Iterator[TestDb]:
    """A fresh, LiveSync-initialised database, dropped after the test."""
    db = TestDb(server=couch_server, name=f"t_{uuid.uuid4().hex}")
    with couch_server.client() as c:
        c.put(f"/{db.name}").raise_for_status()
    init_livesync_db(db)
    yield db
    with couch_server.client() as c:
        c.delete(f"/{db.name}")


@pytest.fixture
def bare_db(couch_server: CouchServer) -> Iterator[TestDb]:
    """A fresh database with no LiveSync control documents, dropped after the test."""
    db = TestDb(server=couch_server, name=f"t_{uuid.uuid4().hex}")
    with couch_server.client() as c:
        c.put(f"/{db.name}").raise_for_status()
    yield db
    with couch_server.client() as c:
        c.delete(f"/{db.name}")
