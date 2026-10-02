"""obsidian-vault-mirror tests import mirror.py directly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
