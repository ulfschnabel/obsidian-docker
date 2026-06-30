import pytest
from pathlib import Path


@pytest.fixture
def vault(tmp_path):
    """Minimal fake vault with a few notes in nested folders."""
    (tmp_path / "note1.md").write_text("# Note 1\nHello world.")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "note2.md").write_text("# Note 2\nNested note.")
    (tmp_path / "subdir" / "deep").mkdir()
    (tmp_path / "subdir" / "deep" / "note3.md").write_text("# Note 3\nDeep note.")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "config").write_text("{}")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "deleted.md").write_text("deleted")
    return tmp_path
