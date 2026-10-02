"""The test harness itself: a throwaway CouchDB with LiveSync control documents."""
import pytest

pytestmark = pytest.mark.couchdb


def test_couch_db_is_livesync_initialised(couch_db):
    with couch_db.client() as c:
        milestone = c.get("/_local/obsydian_livesync_milestone").json()
        version = c.get("/obsydian_livesync_version").json()
    assert milestone["type"] == "milestoneinfo"
    assert milestone["locked"] is False
    assert milestone["tweak_values"]["PREFERRED"]["hashAlg"] == "xxhash64"
    assert version["version"] == 12


def test_recording_client_classifies_reads_and_writes(couch_db):
    rec = couch_db.recording_client()
    with rec.client as c:
        c.get("/")
        c.post("/_all_docs", json={"keys": []})
        c.put("/some-doc", json={"x": 1})
    assert rec.writes() == [("PUT", f"/{couch_db.name}/some-doc")]
