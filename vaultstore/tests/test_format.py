"""Format functions, pinned to documents the real LiveSync 1.0.15 core produced."""
import base64
import hashlib
import json
import unicodedata

import pytest

from vaultstore import format as fmt
from vaultstore.errors import IncompleteNote, InvalidPath


def source_bytes(source: dict) -> bytes:
    return base64.b64decode(source["content_b64"])


def doc_for(golden, path: str) -> dict:
    return golden.docs[fmt.path2id(path)]


class TestPath2Id:
    def test_examples(self):
        assert fmt.path2id("WoW RE/00 Index.md") == "wow re/00 index.md"
        assert fmt.path2id("_inbox/today.md") == "/_inbox/today.md"

    def test_nfd_is_not_normalised(self):
        nfd = unicodedata.normalize("NFD", "NFD/Café résumé.md")
        assert fmt.path2id(nfd) == nfd.lower()
        assert fmt.path2id(nfd) != fmt.path2id(unicodedata.normalize("NFC", nfd))

    def test_matches_every_golden_note(self, golden):
        notes = golden.notes()
        assert len(notes) >= 14
        for d in notes:
            assert fmt.path2id(d["path"]) == d["_id"], d["path"]


class TestCheckPath:
    @pytest.mark.parametrize("bad", ["", "a\0b.md", "Colon/a:b.md", "/abs.md"])
    def test_rejects_paths_the_core_cannot_represent(self, bad):
        with pytest.raises(InvalidPath):
            fmt.check_path(bad)

    def test_accepts_ordinary_paths(self):
        fmt.check_path("Ümlaut Ördner/Größe ✓.md")


class TestChunkId:
    def test_uses_utf16_length(self):
        # "🎉" is one code point but two UTF-16 code units; the core hashes "🎉-2".
        import xxhash
        expected = "h:" + fmt.base36(xxhash.xxh64("🎉-2".encode()).intdigest())
        assert fmt.chunk_id("🎉") == expected

    def test_matches_every_golden_chunk(self, golden):
        leaves = golden.leaves()
        assert len(leaves) > 400
        for d in leaves:
            assert fmt.chunk_id(d["data"]) == d["_id"]


class TestChunkRev:
    def test_device_rule_matches_golden_latin1_chunks(self, golden):
        # For chunks whose characters are all <= U+00FF, the device (UTF-8) rule and the
        # Node CLI's Latin-1 rule agree, so the CLI fixtures pin the device rule exactly.
        latin1 = [d for d in golden.leaves() if all(ord(c) <= 0xFF for c in d["data"])]
        assert len(latin1) > 400
        for d in latin1:
            assert fmt.chunk_rev(d["_id"], d["data"]) == d["_rev"]

    def test_cli_differs_only_by_its_latin1_hashing(self, golden):
        # Documents why the CLI's revisions differ for non-Latin-1 chunks (design D2):
        # Node PouchDB hashes JSON.stringify output as Latin-1-truncated code units.
        def node_rev(d):
            js = json.dumps({"_id": d["_id"], "data": d["data"], "type": "leaf"}, separators=(",", ":"), ensure_ascii=False)
            units = js.encode("utf-16-le")
            return "1-" + hashlib.md5(bytes(units[i] for i in range(0, len(units), 2))).hexdigest()

        for d in golden.leaves():
            assert node_rev(d) == d["_rev"]

    def test_chunk_doc(self):
        doc = fmt.chunk_doc("hello")
        assert doc == {"_id": fmt.chunk_id("hello"), "_rev": fmt.chunk_rev(fmt.chunk_id("hello"), "hello"),
                       "type": "leaf", "data": "hello"}


class TestFileBytes:
    def test_text_is_utf8_as_a_device_writes_it(self):
        assert fmt.file_bytes("Größe ✓\r\n") == "Größe ✓\r\n".encode("utf-8")
        assert fmt.file_bytes("a\ud800b") == "a\ufffdb".encode("utf-8")  # lone surrogate, as TextEncoder

    def test_attachments_are_their_bytes(self):
        assert fmt.file_bytes(b"\x00\xff") == b"\x00\xff"

    def test_size_is_the_file_length(self):
        for content in ("Größe ✓", "", "a\ud800b", b"\x00\x01"):
            assert fmt.content_size(content) == len(fmt.file_bytes(content))


class TestSize:
    def test_utf8_bytes_not_characters(self):
        assert fmt.content_size("Größe ✓") == 11
        assert fmt.content_size(b"\x00\x01") == 2

    def test_matches_golden(self, golden):
        for s in golden.sources:
            if s["path"] == "Colon/a:b.md":
                continue  # the core mangled this path; see TestCheckPath
            d = doc_for(golden, s["path"])
            data = source_bytes(s)
            assert fmt.content_size(data if s["binary"] else data.decode("utf-8")) == d["size"], s["path"]


class TestDecode:
    def test_round_trips_every_golden_source(self, golden):
        chunks = golden.chunk_data()
        checked = 0
        for s in golden.sources:
            if s["path"] == "Colon/a:b.md":
                continue
            d = doc_for(golden, s["path"])
            content = fmt.decode_content(d, chunks)
            expected = source_bytes(s) if s["binary"] else source_bytes(s).decode("utf-8")
            assert content == expected, s["path"]
            checked += 1
        assert checked >= 14

    def test_multi_chunk_binary_decodes_piecewise(self, golden):
        d = golden.docs["attachments/blob.bin"]
        assert d["type"] == "newnote" and len(d["children"]) > 1
        assert len(fmt.decode_content(d, golden.chunk_data())) == 300_000

    def test_missing_chunk_is_incomplete(self, golden):
        d = golden.docs["large/large.md"]
        chunks = dict(golden.chunk_data())
        del chunks[d["children"][3]]
        with pytest.raises(IncompleteNote) as e:
            fmt.decode_content(d, chunks)
        assert e.value.missing == [d["children"][3]]


class TestNoteDoc:
    def test_shape_matches_golden_plain_note(self, golden):
        g = golden.docs["plain/ascii.md"]
        doc = fmt.note_doc("Plain/ascii.md", children=g["children"], size=g["size"], ctime=g["ctime"], mtime=g["mtime"])
        assert set(doc) == set(g) - {"_rev"}
        assert doc == {k: v for k, v in g.items() if k != "_rev"}

    def test_includes_rev_when_updating(self):
        doc = fmt.note_doc("a.md", children=[], size=0, ctime=1, mtime=2, rev="3-x")
        assert doc["_rev"] == "3-x"

    def test_deleted_shape_matches_golden(self, golden):
        g = golden.docs["lifecycle/deleted.md"]
        live = {k: v for k, v in g.items() if k not in ("deleted",)}
        deleted = fmt.mark_deleted(live, mtime=g["mtime"])
        assert deleted == g
        assert deleted["children"] == live["children"]

    def test_is_deleted(self):
        assert fmt.is_deleted({"deleted": True})
        assert fmt.is_deleted({"_deleted": True})
        assert not fmt.is_deleted({"path": "a.md"})
