import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "fixtures" / "livesync-1.0.15.json"


class Golden:
    """Documents the real LiveSync 1.0.15 core produced for the synthetic note set."""

    def __init__(self, data: dict):
        self.raw = data
        self.docs = {d["_id"]: d for d in data["docs"]}
        self.sources = data["sources"]
        self.milestone = data["milestone"]

    def notes(self) -> list[dict]:
        return [d for d in self.docs.values() if d.get("type") in ("plain", "newnote")]

    def leaves(self) -> list[dict]:
        return [d for d in self.docs.values() if d.get("type") == "leaf"]

    def chunk_data(self) -> dict[str, str]:
        return {d["_id"]: d["data"] for d in self.leaves()}


@pytest.fixture(scope="session")
def golden() -> Golden:
    return Golden(json.loads(GOLDEN.read_text()))
