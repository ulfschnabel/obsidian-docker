import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obsidian_remarkable_sync.converter import (
    check_dependencies,
    convert_to_pdf,
    preprocess,
)


# --- preprocess ---

def test_wiki_link_replaced():
    assert preprocess("See [[My Note]] for details.") == "See My Note for details."


def test_wiki_link_alias_replaced():
    assert preprocess("See [[My Note|click here]].") == "See click here."


def test_embed_stripped():
    result = preprocess("Some text\n![[image.png]]\nMore text")
    assert "![[" not in result
    assert "image.png" not in result


def test_front_matter_stripped_title_injected():
    content = "---\ntitle: My Great Note\ntags: [foo]\n---\nBody text."
    result = preprocess(content)
    assert result.startswith("# My Great Note\n\n")
    assert "tags:" not in result
    assert "Body text." in result


def test_front_matter_no_title():
    content = "---\ntags: [foo]\n---\nBody text."
    result = preprocess(content)
    assert not result.startswith("#")
    assert "tags:" not in result
    assert "Body text." in result


def test_no_front_matter_unchanged():
    content = "# Heading\nSome text [[link]] here."
    result = preprocess(content)
    assert "# Heading" in result
    assert "[[" not in result


# --- convert_to_pdf ---

def test_convert_calls_pandoc(tmp_path):
    md_file = tmp_path / "note.md"
    md_file.write_text("# Hello\nWorld")
    out = tmp_path / "note.pdf"

    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("obsidian_remarkable_sync.converter.subprocess.run", return_value=mock_result) as mock_run:
        convert_to_pdf(md_file, out, paper_size="a5")

    args = mock_run.call_args[0][0]
    assert "pandoc" in args
    assert "--pdf-engine=weasyprint" in args
    assert "--variable=papersize:a5" in args
    assert str(out) in args


def test_convert_raises_on_failure(tmp_path):
    md_file = tmp_path / "note.md"
    md_file.write_text("# Hello")
    out = tmp_path / "note.pdf"

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "some pandoc error"

    with patch("obsidian_remarkable_sync.converter.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="pandoc failed"):
            convert_to_pdf(md_file, out)


# --- check_dependencies ---

def test_missing_pandoc_raises():
    with patch("obsidian_remarkable_sync.converter.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="pandoc not found"):
            check_dependencies()


def test_missing_weasyprint_raises():
    with patch("obsidian_remarkable_sync.converter.shutil.which", return_value="/usr/bin/pandoc"):
        with patch("builtins.__import__", side_effect=ImportError):
            with pytest.raises(RuntimeError, match="weasyprint not found"):
                check_dependencies()
