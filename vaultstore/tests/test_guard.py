"""Compatibility guard: refuse rather than write documents devices cannot read."""
import pytest

from vaultstore.errors import IncompatibleVault
from vaultstore.guard import Guard
from vaultstore.testing import init_livesync_db

pytestmark = pytest.mark.couchdb


def guard_for(db, **kw) -> Guard:
    return Guard(db.client(), **kw)


def test_compatible_vault_is_readable_and_writable(couch_db):
    g = guard_for(couch_db)
    s = g.status()
    assert s.writable and s.readable and s.reason is None
    g.ensure_writable()
    g.ensure_readable()


def test_missing_milestone_refuses_writes(bare_db):
    g = guard_for(bare_db)
    with pytest.raises(IncompatibleVault, match="milestone"):
        g.ensure_writable()


def test_locked_remote_refuses_writes_but_allows_reads(bare_db):
    init_livesync_db(bare_db, locked=True)
    g = guard_for(bare_db)
    with pytest.raises(IncompatibleVault, match="locked"):
        g.ensure_writable()
    g.ensure_readable()


@pytest.mark.parametrize("setting, blocks_reads", [
    ("encrypt", True),
    ("enableCompression", True),
    ("usePathObfuscation", False),
    ("handleFilenameCaseSensitive", False),
])
def test_unsupported_setting_refuses_writes(bare_db, setting, blocks_reads):
    init_livesync_db(bare_db, tweaks={setting: True})
    g = guard_for(bare_db)
    with pytest.raises(IncompatibleVault, match=setting):
        g.ensure_writable()
    if blocks_reads:
        with pytest.raises(IncompatibleVault, match=setting):
            g.ensure_readable()
    else:
        g.ensure_readable()


def test_newer_database_version_refuses(bare_db):
    init_livesync_db(bare_db, version=13)
    g = guard_for(bare_db)
    with pytest.raises(IncompatibleVault, match="version 13"):
        g.ensure_writable()


def test_reevaluates_after_ttl(couch_db):
    now = [1000.0]
    g = guard_for(couch_db, ttl_s=60, clock=lambda: now[0])
    g.ensure_writable()
    with couch_db.client() as c:
        m = c.get("/_local/obsydian_livesync_milestone").json()
        m["tweak_values"]["PREFERRED"]["encrypt"] = True
        c.put("/_local/obsydian_livesync_milestone", json=m).raise_for_status()
    now[0] += 59
    g.ensure_writable()  # still the cached evaluation
    now[0] += 2
    with pytest.raises(IncompatibleVault, match="encrypt"):
        g.ensure_writable()


def test_status_reports_the_database_incarnation(bare_db):
    init_livesync_db(bare_db, created=1777986784536, tweaks={"encrypt": True})
    assert guard_for(bare_db).status().incarnation == 1777986784536


def test_no_incarnation_without_a_milestone(bare_db):
    assert guard_for(bare_db).status().incarnation is None


def test_real_cli_milestone_is_compatible(bare_db, golden):
    # The milestone the real 1.0.15 core created (with its own node entry) passes.
    m = dict(golden.milestone)
    m.pop("_rev", None)
    with bare_db.client() as c:
        c.put("/_local/obsydian_livesync_milestone", json=m).raise_for_status()
        c.put("/obsydian_livesync_version", json={"version": 12, "type": "versioninfo"}).raise_for_status()
    guard_for(bare_db).ensure_writable()
