"""Read conformance: the real LiveSync core reads what vaultstore writes (design D13).

Opt-in by marker; needs docker and the CLI image. Re-run with
VAULTSTORE_CLI_IMAGE set to a newer tag before upgrading device plugins.
"""
import base64

import pytest

from vaultstore.store import Store

pytestmark = [pytest.mark.couchdb, pytest.mark.conformance]


def test_core_reads_what_vaultstore_writes(livesync_cli, golden):
    cli, db = livesync_cli
    cli.run("sync")  # the core initialises the empty remote (milestone, version document)
    store = Store(db.client())
    store.guard.ensure_writable()  # the real core's milestone passes our guard

    expected: dict[str, bytes] = {}
    for s in golden.sources:
        if s["binary"] or s.get("lifecycle") or s["path"] == "Colon/a:b.md":
            continue
        content = base64.b64decode(s["content_b64"])
        store.write(s["path"], content.decode("utf-8"))
        expected[s["path"]] = content

    rev = store.write("Lifecycle/updated.md", "Version one.\n")
    store.write("Lifecycle/updated.md", "Version two, longer.\n", expected_revision=rev)
    expected["Lifecycle/updated.md"] = b"Version two, longer.\n"
    rev = store.write("Lifecycle/deleted.md", "Soon deleted.\n")
    store.delete("Lifecycle/deleted.md", rev)

    cli.run("sync")

    files = cli.mirror()
    assert sorted(files) == sorted(expected)  # the deleted note is not materialised
    for path, content in expected.items():
        assert files[path] == content, path

    listed = {line.split("\t")[0] for line in cli.run("ls").stdout.decode("utf-8").splitlines() if line}
    assert listed == set(expected)

    # The core took the documents as they are: another full cycle rewrites none of them.
    revisions = {p: store.raw_doc(p)["_rev"] for p in [*expected, "Lifecycle/deleted.md"]}
    cli.run("sync")
    assert {p: store.raw_doc(p)["_rev"] for p in revisions} == revisions
