"""Generate golden fixtures by having the real LiveSync core write synthetic notes.

Runs `ghcr.io/vrtmrz/livesync-cli:<tag>` (same core as the device plugin) against
a throwaway CouchDB on a private docker network, then exports every document
the core produced. Nothing touches the host filesystem except the output JSON:
the CLI's local database lives in a docker volume created and removed here.

Usage:  .venv/bin/python vaultstore/tests/fixtures/generate_fixtures.py [cli-tag]
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zlib
from pathlib import Path

import httpx

CLI_TAG = sys.argv[1] if len(sys.argv) > 1 else "1.0.15-cli"
CLI_IMAGE = f"ghcr.io/vrtmrz/livesync-cli:{CLI_TAG}"
OUT = Path(__file__).with_name(f"livesync-{CLI_TAG.removesuffix('-cli')}.json")
FIXED_MTIME = 1767225600  # 2026-01-01T00:00:00Z, so pushes are deterministic

ADMIN, PASSWORD, DB = "admin", "testpass", "fixtures"


def png_1x1() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + tag + data + zlib.crc32(tag + data).to_bytes(4, "big")
    ihdr = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def large_binary() -> bytes:
    """300 KB of deterministic, incompressible bytes (a SHA-256 chain)."""
    out, h = bytearray(), b"vaultstore"
    while len(out) < 300_000:
        h = hashlib.sha256(h).digest()
        out += h
    return bytes(out[:300_000])


def large_note() -> str:
    paras = []
    for i in range(1, 1200):
        paras.append(f"## Section {i}\n\nParagraph {i}: " + ("lorem ipsum dolor sit amet " * 6).strip() + ".")
    return "# Large note\n\n" + "\n\n".join(paras) + "\n"


# vault path -> content (str = UTF-8 text, bytes = binary attachment)
SOURCES: dict[str, str | bytes] = {
    "Plain/ascii.md": "# ASCII\n\nFirst paragraph.\n\nSecond paragraph with a [[link]] and #tag.\n",
    "Ümlaut Ördner/Größe ✓.md": "# Größe ✓\n\nÄpfel, Öl, Übermut — naïve café, 日本語テキスト.\n",
    "CRLF/windows.md": "# CRLF\r\n\r\nLine one\r\nLine two\r\n",
    "Emoji/🎉 party.md": "# 🎉 Party\n\n👩‍💻 writes 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 and 🏳️‍🌈 flags.\n",
    "_underscore/leading.md": "Leading underscore path.\n",
    "MixedCase/CamelCase Note.md": "Mixed case path.\n",
    "Empty/empty.md": "",
    "LongLines/long.md": "x" * 5000 + "\n" + "y" * 300 + "\n",
    "Large/large.md": large_note(),
    unicodedata.normalize("NFD", "NFD/Café résumé.md"): "Path given in NFD form.\n",
    "Colon/a:b.md": "Colon in the file name.\n",
    "Attachments/pixel.png": png_1x1(),
    "Attachments/blob.bin": large_binary(),
}
UPDATED_PATH, UPDATED_V1, UPDATED_V2 = "Lifecycle/updated.md", "Version one.\n", "Version two, longer.\n"
DELETED_PATH, DELETED_CONTENT = "Lifecycle/deleted.md", "Soon deleted.\n"


def sh(*args: str, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(args, capture_output=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{r.stdout.decode(errors='replace')}\n{r.stderr.decode(errors='replace')}")
    return r


def main() -> None:
    run_id = uuid.uuid4().hex[:8]
    net, couch, vol = f"vsfix-{run_id}", f"vsfix-couch-{run_id}", f"vsfix-data-{run_id}"
    results: dict[str, str] = {}
    try:
        sh("docker", "network", "create", net)
        sh("docker", "volume", "create", vol)
        sh("docker", "run", "-d", "--name", couch, "--network", net, "-p", "127.0.0.1::5984",
           "-e", f"COUCHDB_USER={ADMIN}", "-e", f"COUCHDB_PASSWORD={PASSWORD}", "couchdb:3")
        port = sh("docker", "port", couch, "5984/tcp").stdout.decode().split()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        for _ in range(180):
            try:
                if httpx.get(base, timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        httpx.put(f"{base}/{DB}", auth=(ADMIN, PASSWORD)).raise_for_status()

        def cli(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            return sh("docker", "run", "--rm", "--network", net, "-v", f"{vol}:/data", CLI_IMAGE, *args, check=check)

        # Settings: CLI defaults already match the vault's required tweaks; add the connection.
        cli("init-settings", "/data/.livesync/settings.json")
        raw = sh("docker", "run", "--rm", "-v", f"{vol}:/data", "--entrypoint", "cat", CLI_IMAGE,
                 "/data/.livesync/settings.json").stdout
        settings = json.loads(raw)
        settings.update(couchDB_URI=f"http://{couch}:5984", couchDB_USER=ADMIN, couchDB_PASSWORD=PASSWORD,
                        couchDB_DBNAME=DB, isConfigured=True, liveSync=False)
        sh("docker", "run", "--rm", "-i", "-v", f"{vol}:/data", "--entrypoint", "sh", CLI_IMAGE, "-c",
           "cat > /data/.livesync/settings.json", input=json.dumps(settings).encode())

        # Stage source files into the volume with fixed mtimes, then push each.
        with tempfile.TemporaryDirectory() as tmp:
            staged: list[tuple[str, str]] = []
            items = list(SOURCES.items()) + [(UPDATED_PATH, UPDATED_V1), (DELETED_PATH, DELETED_CONTENT)]
            for i, (vpath, content) in enumerate(items):
                f = Path(tmp) / f"src{i}"
                f.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
                os.utime(f, (FIXED_MTIME, FIXED_MTIME))
                staged.append((f"/data/src/src{i}", vpath))
            v2 = Path(tmp) / "updated_v2"
            v2.write_bytes(UPDATED_V2.encode())
            os.utime(v2, (FIXED_MTIME + 60, FIXED_MTIME + 60))
            helper = f"vsfix-cp-{run_id}"
            sh("docker", "create", "--name", helper, "-v", f"{vol}:/data", "--entrypoint", "true", CLI_IMAGE)
            try:
                sh("docker", "cp", f"{tmp}/.", f"{helper}:/data/src/")
            finally:
                sh("docker", "rm", "-f", helper, check=False)

        for src, vpath in staged:
            r = cli("push", src, vpath, check=False)
            results[vpath] = "ok" if r.returncode == 0 else f"push failed: {r.stderr.decode(errors='replace')[-300:]}"
        cli("sync")
        cli("push", "/data/src/updated_v2", UPDATED_PATH)
        cli("rm", DELETED_PATH)
        cli("sync")

        auth = (ADMIN, PASSWORD)
        docs = [r["doc"] for r in httpx.get(f"{base}/{DB}/_all_docs", params={"include_docs": "true"}, auth=auth).json()["rows"]
                if not r["id"].startswith("_design/")]
        milestone = httpx.get(f"{base}/{DB}/_local/obsydian_livesync_milestone", auth=auth).json()
        sources = [
            {"path": p, "binary": isinstance(c, bytes),
             "content_b64": base64.b64encode(c if isinstance(c, bytes) else c.encode("utf-8")).decode(),
             "push": results.get(p, "ok")}
            for p, c in SOURCES.items()
        ]
        sources.append({"path": UPDATED_PATH, "binary": False, "content_b64": base64.b64encode(UPDATED_V2.encode()).decode(),
                        "push": results.get(UPDATED_PATH, "ok"), "lifecycle": "updated",
                        "previous_b64": base64.b64encode(UPDATED_V1.encode()).decode()})
        sources.append({"path": DELETED_PATH, "binary": False, "content_b64": base64.b64encode(DELETED_CONTENT.encode()).decode(),
                        "push": results.get(DELETED_PATH, "ok"), "lifecycle": "deleted"})
        OUT.write_text(json.dumps({
            "generator": {"image": CLI_IMAGE, "script": Path(__file__).name, "fixed_mtime_s": FIXED_MTIME},
            "sources": sources,
            "milestone": milestone,
            "docs": sorted(docs, key=lambda d: d["_id"]),
        }, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"wrote {OUT} ({len(docs)} docs)")
        for p, r in results.items():
            if r != "ok":
                print(f"  {p!r}: {r}")
    finally:
        sh("docker", "rm", "-f", couch, check=False)
        sh("docker", "volume", "rm", vol, check=False)
        sh("docker", "network", "rm", net, check=False)


if __name__ == "__main__":
    main()
