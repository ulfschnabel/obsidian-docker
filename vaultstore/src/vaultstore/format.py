"""Pure functions reproducing the Self-hosted LiveSync document format.

Pinned by golden fixtures written by the real LiveSync 1.0.15 core and by an
opt-in check against the production database (see tests/). The rules here are
the plugin's, not ours: change them only when the fixtures say so.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable, Mapping

import xxhash

from .errors import IncompleteNote, InvalidPath

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def base36(n: int) -> str:
    """JavaScript's BigInt.prototype.toString(36)."""
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out or "0"


def _js_utf8(s: str) -> bytes:
    """UTF-8 bytes as JavaScript's TextEncoder produces them (lone surrogates -> U+FFFD)."""
    return _LONE_SURROGATE.sub("�", s).encode("utf-8")


def _js_json(obj: object) -> str:
    """JSON.stringify for plain objects of strings (ES2019: lone surrogates escaped)."""
    s = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", s)


def utf16_len(s: str) -> int:
    """JavaScript's String.prototype.length: UTF-16 code units."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def path2id(path: str) -> str:
    """Document id for a vault path (handleFilenameCaseSensitive off, no obfuscation).

    No Unicode normalisation: the core stores and ids NFD paths unchanged.
    """
    lowered = path.lower()
    return "/" + lowered if lowered.startswith("_") else lowered


def check_path(path: str) -> None:
    """Reject paths the LiveSync core cannot represent as a note id."""
    if not path:
        raise InvalidPath("empty path")
    if "\0" in path:
        raise InvalidPath(f"NUL character in {path!r}")
    if ":" in path:
        # The core treats ':' as an id-namespace separator (h:, i:, ps:): 'Colon/a:b.md' became id 'b.md'.
        raise InvalidPath(f"':' is not allowed in note paths: {path!r}")
    if path.startswith("/"):
        raise InvalidPath(f"absolute path not allowed: {path!r}")


MAX_PIECE_UNITS = 4000
_PARAGRAPH_END = re.compile(r"\r?\n(?:[ \t]*\r?\n)+")
_LINE_END = re.compile(r"(?<=\n)")


def split_text(text: str) -> list[str]:
    """Split note text into chunk pieces whose concatenation is exactly `text`.

    Pieces end after a run of blank lines, so editing one paragraph changes one
    piece and the rest dedupe. Paragraphs over MAX_PIECE_UNITS break on line
    ends, and over-long lines break by code point (never inside a surrogate
    pair). This is not the core's Rabin-Karp splitter; readers never verify
    piece boundaries, so this affects deduplication only (design D2).
    """
    pieces: list[str] = []
    start = 0
    for m in _PARAGRAPH_END.finditer(text):
        pieces.extend(_cap_paragraph(text[start:m.end()]))
        start = m.end()
    if start < len(text):
        pieces.extend(_cap_paragraph(text[start:]))
    return pieces


def _cap_paragraph(paragraph: str) -> list[str]:
    if utf16_len(paragraph) <= MAX_PIECE_UNITS:
        return [paragraph]
    out: list[str] = []
    buf, buf_units = "", 0
    for line in _LINE_END.split(paragraph):
        if not line:
            continue
        units = utf16_len(line)
        if buf and buf_units + units > MAX_PIECE_UNITS:
            out.append(buf)
            buf, buf_units = "", 0
        if units > MAX_PIECE_UNITS:
            out.extend(_cap_line(line))
            continue
        buf += line
        buf_units += units
    if buf:
        out.append(buf)
    return out


def _cap_line(line: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    units = 0
    for ch in line:
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > MAX_PIECE_UNITS:
            out.append("".join(buf))
            buf, units = [], 0
        buf.append(ch)
        units += w
    if buf:
        out.append("".join(buf))
    return out


def chunk_id(piece: str) -> str:
    return "h:" + base36(xxhash.xxh64(_js_utf8(f"{piece}-{utf16_len(piece)}")).intdigest())


def chunk_rev(cid: str, data: str) -> str:
    """Deterministic revision as device (browser/Electron) PouchDB assigns it."""
    return "1-" + hashlib.md5(_js_utf8(_js_json({"_id": cid, "data": data, "type": "leaf"}))).hexdigest()


def chunk_doc(piece: str) -> dict:
    cid = chunk_id(piece)
    return {"_id": cid, "_rev": chunk_rev(cid, piece), "type": "leaf", "data": piece}


def content_size(content: str | bytes) -> int:
    """`size` as the core records it: bytes of the decoded content (UTF-8 for text)."""
    return len(content) if isinstance(content, bytes) else len(_js_utf8(content))


def note_doc(
    path: str,
    *,
    children: Iterable[str],
    size: int,
    ctime: int,
    mtime: int,
    type: str = "plain",
    rev: str | None = None,
) -> dict:
    doc = {
        "_id": path2id(path),
        "path": path,
        "children": list(children),
        "ctime": ctime,
        "mtime": mtime,
        "size": size,
        "type": type,
        "eden": {},
    }
    if rev is not None:
        doc["_rev"] = rev
    return doc


def mark_deleted(doc: Mapping, *, mtime: int) -> dict:
    """Logical deletion as the core performs it: children are kept."""
    return {**doc, "deleted": True, "mtime": mtime}


def is_deleted(doc: Mapping) -> bool:
    return bool(doc.get("deleted") or doc.get("_deleted"))


def decode_content(note: Mapping, chunk_data: Mapping[str, str]) -> str | bytes:
    """Concatenate a note's chunks in `children` order.

    Text notes join the pieces. Binary notes ("newnote") carry one base64 string
    per chunk, each with its own padding, so each piece is decoded separately.
    Chunks are looked up in `chunk_data`, then in the note's legacy `eden`.
    """
    eden = note.get("eden") or {}
    pieces: list[str] = []
    missing: list[str] = []
    for cid in note.get("children", []):
        if cid in chunk_data:
            pieces.append(chunk_data[cid])
        elif cid in eden and "data" in eden[cid]:
            pieces.append(eden[cid]["data"])
        else:
            missing.append(cid)
    if missing:
        raise IncompleteNote.for_path(note.get("path", note.get("_id", "?")), missing)
    if note.get("type") == "newnote":
        return b"".join(base64.b64decode(p) for p in pieces)
    return "".join(pieces)
