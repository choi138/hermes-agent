"""Keep standalone ops imports local; every subprocess HOME remains disposable."""
from pathlib import Path
import sys

import pytest

OPS = Path(__file__).resolve().parents[3] / "scripts/ops"
for directory in (OPS / "review-gate", OPS / "bg-continue-hook", Path(__file__).parent / "review_gate"):
    sys.path.insert(0, str(directory))


@pytest.fixture(autouse=True)
def ops_home(tmp_path, monkeypatch):
    home = tmp_path / "ops-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
