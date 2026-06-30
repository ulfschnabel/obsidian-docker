from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from obsidian_remarkable_sync.uploader import (
    check_auth,
    delete_remote,
    ensure_folder,
    upload_pdf,
)


def make_config(tmp_path, authenticated=True):
    config_dir = tmp_path / "rmapi-config"
    config_dir.mkdir()
    if authenticated:
        (config_dir / "rmapi.conf").write_text('{"token": "fake"}')
    return config_dir


def mock_run(returncode=0, stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    return result


# --- check_auth ---

def test_check_auth_passes(tmp_path):
    config = make_config(tmp_path)
    check_auth(config)  # Should not raise


def test_check_auth_fails_no_token(tmp_path):
    config = make_config(tmp_path, authenticated=False)
    with pytest.raises(RuntimeError, match="rmapi not authenticated"):
        check_auth(config)


# --- ensure_folder ---

def test_ensure_folder_calls_mkdir_for_each_segment(tmp_path):
    config = make_config(tmp_path)
    with patch("obsidian_remarkable_sync.uploader.subprocess.run", return_value=mock_run()) as mock:
        ensure_folder("Obsidian/Reading/Philosophy", rmapi_config=config)

    calls = [c[0][0] for c in mock.call_args_list]
    assert ["rmapi", "mkdir", "Obsidian"] in calls
    assert ["rmapi", "mkdir", "Obsidian/Reading"] in calls
    assert ["rmapi", "mkdir", "Obsidian/Reading/Philosophy"] in calls


# --- upload_pdf ---

def test_upload_pdf_success(tmp_path):
    config = make_config(tmp_path)
    pdf = tmp_path / "note.pdf"
    pdf.write_bytes(b"%PDF")

    with patch("obsidian_remarkable_sync.uploader.subprocess.run", return_value=mock_run()):
        upload_pdf(pdf, "Obsidian/note", rmapi_config=config)


def test_upload_pdf_raises_on_failure(tmp_path):
    config = make_config(tmp_path)
    pdf = tmp_path / "note.pdf"
    pdf.write_bytes(b"%PDF")

    with patch("obsidian_remarkable_sync.uploader.subprocess.run", return_value=mock_run(returncode=1, stderr="connection error")):
        with pytest.raises(RuntimeError, match="Upload failed"):
            upload_pdf(pdf, "Obsidian/note", rmapi_config=config)


# --- delete_remote ---

def test_delete_remote_success(tmp_path):
    config = make_config(tmp_path)
    with patch("obsidian_remarkable_sync.uploader.subprocess.run", return_value=mock_run()):
        delete_remote("Obsidian/note", rmapi_config=config)  # Should not raise


def test_delete_remote_logs_warning_on_not_found(tmp_path, caplog):
    import logging
    config = make_config(tmp_path)
    with patch("obsidian_remarkable_sync.uploader.subprocess.run", return_value=mock_run(returncode=1, stderr="not found")):
        with caplog.at_level(logging.WARNING):
            delete_remote("Obsidian/note", rmapi_config=config)
    assert "may already be absent" in caplog.text
