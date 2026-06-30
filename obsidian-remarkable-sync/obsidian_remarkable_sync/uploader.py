from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _rmapi(*args: str, rmapi_config: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["RMAPI_CONFIG"] = str(rmapi_config / "rmapi.conf")
    return subprocess.run(
        ["rmapi", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def check_auth(rmapi_config: Path) -> None:
    token_file = rmapi_config / "rmapi.conf"
    if not token_file.exists():
        raise RuntimeError(
            f"rmapi not authenticated — run rmapi once to pair with your reMarkable account "
            f"(RMAPI_CONFIG dir: {rmapi_config})"
        )


def ensure_folder(remote_path: str, *, rmapi_config: Path) -> None:
    """Create each segment of remote_path on reMarkable if it doesn't exist."""
    parts = [p for p in remote_path.strip("/").split("/") if p]
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        result = _rmapi("mkdir", current, rmapi_config=rmapi_config)
        # rmapi mkdir exits 0 even if the folder already exists; non-zero is a real error
        if result.returncode != 0:
            log.warning("rmapi mkdir %s: %s", current, result.stderr.strip())


def upload_pdf(pdf: Path, remote_folder: str, *, rmapi_config: Path) -> None:
    result = _rmapi("put", "--force", str(pdf), remote_folder, rmapi_config=rmapi_config)
    if result.returncode != 0:
        raise RuntimeError(
            f"Upload failed for {pdf.name}: {result.stderr.strip()}"
        )
    log.info("Uploaded: %s → %s/", pdf.name, remote_folder)


def delete_remote(remote_path: str, *, rmapi_config: Path) -> None:
    result = _rmapi("rm", remote_path, rmapi_config=rmapi_config)
    if result.returncode != 0:
        log.warning("rmapi rm %s failed (may already be absent): %s", remote_path, result.stderr.strip())
    else:
        log.info("Deleted remote: %s", remote_path)
