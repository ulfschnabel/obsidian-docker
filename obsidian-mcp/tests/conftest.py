"""obsidian-mcp tests import the tool logic directly; server.py (model, Chroma) is never imported."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
