"""Opt-in: reproduce every document in a production export (never committed).

Create the export with:
  curl -s -u USER:PASS 'http://.../obsidian/_all_docs?include_docs=true' > /some/private/path.json
and run with VAULT_EXPORT=/some/private/path.json.
"""
import json
import os

import pytest

from vaultstore import format as fmt

EXPORT = os.environ.get("VAULT_EXPORT")
pytestmark = [
    pytest.mark.export,
    pytest.mark.skipif(not EXPORT, reason="set VAULT_EXPORT to a production _all_docs?include_docs=true export"),
]


@pytest.fixture(scope="module")
def export():
    rows = json.load(open(EXPORT))["rows"]
    docs = {r["id"]: r["doc"] for r in rows}
    notes = [d for d in docs.values() if d.get("type") in ("plain", "newnote")]
    leaves = [d for d in docs.values() if d.get("type") == "leaf"]
    return docs, notes, leaves


def test_note_ids(export):
    _, notes, _ = export
    assert notes
    assert [d["_id"] for d in notes if fmt.path2id(d["path"]) != d["_id"]] == []


def test_chunk_ids_and_device_revisions(export):
    _, _, leaves = export
    assert leaves
    assert [d["_id"] for d in leaves if fmt.chunk_id(d["data"]) != d["_id"]] == []
    assert [d["_id"] for d in leaves if fmt.chunk_rev(d["_id"], d["data"]) != d["_rev"]] == []


def test_every_live_note_round_trips_with_matching_size(export):
    docs, notes, leaves = export
    chunks = {d["_id"]: d["data"] for d in leaves}
    live = [d for d in notes if not fmt.is_deleted(d)]
    assert live
    for d in live:
        content = fmt.decode_content(d, chunks)
        assert fmt.content_size(content) == d["size"], d["path"]
