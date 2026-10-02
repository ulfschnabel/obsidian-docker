"""The text splitter: our own (design D2); splitting affects deduplication, never correctness."""
import base64

import pytest

from vaultstore import format as fmt

SAMPLES = [
    "",
    "single line, no newline",
    "a\n\nb\n\nc",
    "a\r\n\r\nb\r\n",
    "trailing blank lines\n\n\n\n",
    "\n\n\nleading blank lines",
    "spaces on the blank line\n  \t\nnext",
    "x" * 10_000,
    "🎉" * 3_000,
    ("line " * 50 + "\n") * 400,
]


def all_samples(golden):
    out = list(SAMPLES)
    for s in golden.sources:
        if not s["binary"]:
            out.append(base64.b64decode(s["content_b64"]).decode("utf-8"))
    return out


def test_empty_content_has_no_pieces():
    assert fmt.split_text("") == []


def test_round_trip_is_exact(golden):
    for text in all_samples(golden):
        assert "".join(fmt.split_text(text)) == text


def test_deterministic(golden):
    for text in all_samples(golden):
        assert fmt.split_text(text) == fmt.split_text(text)


def test_pieces_respect_the_cap(golden):
    for text in all_samples(golden):
        for p in fmt.split_text(text):
            assert 0 < fmt.utf16_len(p) <= fmt.MAX_PIECE_UNITS


def test_never_splits_a_surrogate_pair():
    for p in fmt.split_text("🎉" * 3_000):  # 6000 UTF-16 units on one line
        p.encode("utf-8")  # a lone surrogate would raise
        assert fmt.utf16_len(p) % 2 == 0


@pytest.mark.parametrize(
    "text, pieces",
    [
        ("a\n\nb\n\nc", ["a\n\n", "b\n\n", "c"]),
        ("a\r\n\r\nb", ["a\r\n\r\n", "b"]),
        ("a\n\n\n\nb", ["a\n\n\n\n", "b"]),
        ("a\nb\nc", ["a\nb\nc"]),
        ("a\n  \nb", ["a\n  \n", "b"]),
    ],
)
def test_breaks_after_blank_lines(text, pieces):
    assert fmt.split_text(text) == pieces


def test_oversized_paragraph_breaks_on_lines():
    para = "".join(f"line {i:04d} " + "x" * 90 + "\n" for i in range(100))  # ~10k units, no blank lines
    pieces = fmt.split_text(para)
    assert len(pieces) > 1
    assert all(p.endswith("\n") for p in pieces)


def test_editing_one_paragraph_changes_one_piece():
    paras = [f"Paragraph {i}: " + "words " * 30 for i in range(10)]
    before = fmt.split_text("\n\n".join(paras))
    paras[5] += " edited"
    after = fmt.split_text("\n\n".join(paras))
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(before) == len(after) and changed == [5]
