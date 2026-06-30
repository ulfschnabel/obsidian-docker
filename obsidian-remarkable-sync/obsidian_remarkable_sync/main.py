from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from obsidian_remarkable_sync.converter import (
    check_dependencies,
    convert_notes_to_pdf,
    convert_to_pdf,
)
from obsidian_remarkable_sync.state import Manifest, compute_hash
from obsidian_remarkable_sync.uploader import (
    check_auth,
    delete_remote,
    ensure_folder,
    upload_pdf,
)
from obsidian_remarkable_sync.vault import NoteRecord, enumerate_notes
from obsidian_remarkable_sync.watcher import VaultWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


def _group_key(note: NoteRecord) -> str:
    """Parent directory path (relative to vault), or note stem for root-level notes.
    e.g. 'Interview Prep/Data Engineering' or 'Coffee & Tea Diary'."""
    parts = Path(note.relative_path).parts
    if len(parts) == 1:
        return Path(parts[0]).stem
    return "/".join(parts[:-1])


def group_notes(notes: list[NoteRecord]) -> dict[str, list[NoteRecord]]:
    groups: dict[str, list[NoteRecord]] = {}
    for note in notes:
        key = _group_key(note)
        groups.setdefault(key, []).append(note)
    return groups


def _group_hash(notes: list[NoteRecord]) -> str:
    parts = "|".join(
        f"{n.relative_path}:{compute_hash(n.absolute_path)}"
        for n in sorted(notes, key=lambda n: n.relative_path)
    )
    return hashlib.sha256(parts.encode()).hexdigest()


def sync_group(
    group_key: str,
    notes: list[NoteRecord],
    manifest: Manifest,
    rmapi_config: Path,
    remarkable_root: str,
    paper_size: str,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    combined_hash = _group_hash(notes)
    existing = manifest.get(group_key)

    if not force and existing and existing["sha256"] == combined_hash:
        log.info("[SKIP] %s (%d notes)", group_key, len(notes))
        return

    pdf_name = Path(group_key).name + ".pdf"
    # Remote folder: remarkable_root for top-level groups, or remarkable_root/<parent> for nested
    key_parts = group_key.split("/")
    if len(key_parts) == 1:
        remote_folder = remarkable_root
    else:
        remote_folder = remarkable_root + "/" + "/".join(key_parts[:-1])
    remote_path = f"{remote_folder}/{Path(group_key).name}"

    if dry_run:
        action = "UPDATE" if existing else "UPLOAD"
        print(f"[{action}] {pdf_name} ({len(notes)} notes) → {remote_folder}/")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = Path(tmpdir) / pdf_name
        try:
            if len(notes) == 1:
                convert_to_pdf(notes[0].absolute_path, pdf_path, paper_size=paper_size)
            else:
                convert_notes_to_pdf(notes, pdf_path, paper_size=paper_size)
        except RuntimeError as e:
            log.error("Conversion failed for %s: %s", group_key, e)
            return

        ensure_folder(remote_folder, rmapi_config=rmapi_config)
        try:
            upload_pdf(pdf_path, remote_folder, rmapi_config=rmapi_config)
        except RuntimeError as e:
            log.error("%s", e)
            return

    manifest.put(group_key, combined_hash, remote_path)


def run_full_sync(
    vault_root: Path,
    manifest: Manifest,
    rmapi_config: Path,
    remarkable_root: str,
    paper_size: str,
    dry_run: bool = False,
    force: bool = False,
    upload_delay: float = 2.0,
) -> None:
    notes = enumerate_notes(vault_root)
    groups = group_notes(notes)
    log.info("Full sync: %d notes → %d groups", len(notes), len(groups))
    first = True
    for group_key, group_notes_list in sorted(groups.items()):
        if not dry_run and not first:
            time.sleep(upload_delay)
        sync_group(
            group_key, group_notes_list, manifest, rmapi_config,
            remarkable_root, paper_size, dry_run=dry_run, force=force,
        )
        first = False


def make_sync_batch(
    vault_root: Path,
    manifest: Manifest,
    rmapi_config: Path,
    remarkable_root: str,
    paper_size: str,
):
    def sync_batch(changed: set[Path], deleted: set[Path]) -> None:
        affected_keys: set[str] = set()

        for abs_path in changed:
            if abs_path.exists():
                rel = str(abs_path.relative_to(vault_root))
                s = abs_path.stat()
                note = NoteRecord(
                    relative_path=rel,
                    absolute_path=abs_path,
                    size_bytes=s.st_size,
                    modified_at=datetime.fromtimestamp(s.st_mtime, tz=timezone.utc),
                )
                affected_keys.add(_group_key(note))

        for abs_path in deleted:
            rel = str(abs_path.relative_to(vault_root))
            parts = Path(rel).parts
            if len(parts) == 1:
                affected_keys.add(Path(parts[0]).stem)
            else:
                affected_keys.add("/".join(parts[:-1]))

        if not affected_keys:
            return

        # Re-enumerate the full vault to get current state of each affected group
        all_notes = enumerate_notes(vault_root)
        groups = group_notes(all_notes)

        for key in sorted(affected_keys):
            if key in groups:
                sync_group(
                    key, groups[key], manifest, rmapi_config,
                    remarkable_root, paper_size,
                )
            else:
                # All notes in this group were deleted
                entry = manifest.get(key)
                if entry:
                    delete_remote(entry["remote_path"], rmapi_config=rmapi_config)
                    manifest.delete(key)

    return sync_batch


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Obsidian vault to reMarkable")
    parser.add_argument("--vault", default=os.environ.get("VAULT_PATH", "/vault"))
    parser.add_argument("--rmapi-config", default=os.environ.get("RMAPI_CONFIG", "/rmapi-config"))
    parser.add_argument("--manifest", default=os.environ.get("MANIFEST_PATH", "/state/manifest.json"))
    parser.add_argument("--remarkable-root", default=os.environ.get("REMARKABLE_ROOT", "Obsidian"))
    parser.add_argument("--paper-size", default=os.environ.get("PAPER_SIZE", "a5"))
    parser.add_argument("--debounce", type=float, default=float(os.environ.get("DEBOUNCE_SECONDS", "60")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reset-manifest", action="store_true")
    args = parser.parse_args()

    vault_root = Path(args.vault)
    rmapi_config = Path(args.rmapi_config)
    manifest_path = Path(args.manifest)

    if args.reset_manifest:
        manifest = Manifest(manifest_path)
        manifest.reset()
        print("Manifest reset. Next run will perform a full sync.")
        sys.exit(0)

    check_dependencies()
    if not args.dry_run:
        check_auth(rmapi_config)

    manifest = Manifest(manifest_path)

    run_full_sync(
        vault_root, manifest, rmapi_config,
        args.remarkable_root, args.paper_size,
        dry_run=args.dry_run, force=args.force,
    )

    if args.dry_run or args.force:
        sys.exit(0)

    sync_batch_fn = make_sync_batch(vault_root, manifest, rmapi_config, args.remarkable_root, args.paper_size)
    watcher = VaultWatcher(vault_root, sync_batch_fn, debounce_seconds=args.debounce)
    watcher.start()

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("Received signal %s — shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    stop_event.wait()
    watcher.stop()
    log.info("Exited cleanly")


if __name__ == "__main__":
    main()
