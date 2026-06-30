from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)
_WIKI_LINK_ALIAS_RE = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_EMBED_RE = re.compile(r"!\[\[[^\]]+\]\]")


def check_dependencies() -> None:
    if not shutil.which("pandoc"):
        raise RuntimeError(
            "pandoc not found. Install it with: apt-get install pandoc"
        )
    try:
        import weasyprint  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "weasyprint not found. Install it with: pip install weasyprint"
        )


def preprocess(content: str) -> str:
    title_heading = ""
    match = _FRONT_MATTER_RE.match(content)
    if match:
        front_matter = match.group(1)
        title_match = _TITLE_RE.search(front_matter)
        if title_match:
            title_heading = f"# {title_match.group(1).strip()}\n\n"
        content = content[match.end():]

    # Strip image embeds before wiki-links so ![[...]] doesn't match [[...]]
    def strip_embed(m: re.Match) -> str:
        log.debug("Stripping embed: %s", m.group(0))
        return ""

    content = _EMBED_RE.sub(strip_embed, content)
    content = _WIKI_LINK_ALIAS_RE.sub(lambda m: m.group(2), content)
    content = _WIKI_LINK_RE.sub(lambda m: m.group(1), content)

    return title_heading + content


def convert_notes_to_pdf(notes: list, output_path: Path, paper_size: str = "a5") -> None:
    """Merge multiple NoteRecords into one PDF, one H1 section per note."""
    sections = []
    for note in sorted(notes, key=lambda n: n.relative_path):
        content = note.absolute_path.read_text(encoding="utf-8")
        processed = preprocess(content)
        if not processed.lstrip().startswith("# "):
            processed = f"# {Path(note.relative_path).stem}\n\n{processed}"
        sections.append(processed)

    combined = "\n\n---\n\n".join(sections)

    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(combined)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [
                "pandoc",
                str(tmp_path),
                "--pdf-engine=weasyprint",
                f"--variable=papersize:{paper_size}",
                "-o", str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pandoc failed for merged PDF: {result.stderr.strip()}"
            )
    finally:
        tmp_path.unlink(missing_ok=True)


def convert_to_pdf(md_path: Path, output_path: Path, paper_size: str = "a5") -> None:
    preprocessed = preprocess(md_path.read_text(encoding="utf-8"))

    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(preprocessed)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [
                "pandoc",
                str(tmp_path),
                "--pdf-engine=weasyprint",
                f"--variable=papersize:{paper_size}",
                "-o", str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pandoc failed for {md_path.name}: {result.stderr.strip()}"
            )
    finally:
        tmp_path.unlink(missing_ok=True)
