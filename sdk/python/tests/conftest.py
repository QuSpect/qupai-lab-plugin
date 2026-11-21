from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_instrucore import FakeInstruCore  # noqa: E402

from qupai_lab import Lab, config  # noqa: E402


@pytest.fixture
def fake() -> Iterator[FakeInstruCore]:
    server = FakeInstruCore().start()
    yield server
    server.stop()


@pytest.fixture
def lab(fake: FakeInstruCore, tmp_path: Path) -> Lab:
    lab = Lab(fake.url, "qpk_test", record=tmp_path / "calls.jsonl", script_ref="experiments/x/run.py@abc1234")
    lab.poll_s = 0
    lab._sleep = lambda s: None
    return lab


@pytest.fixture(autouse=True)
def _no_ambient_lab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test reads the developer's own QUPAI_LAB_* settings or /workspace/project/.env."""
    monkeypatch.delenv("QUPAI_LAB_URL", raising=False)
    monkeypatch.delenv("QUPAI_LAB_KEY", raising=False)
    monkeypatch.setattr(config, "PROJECT_ENV", tmp_path / "no-project" / ".env")


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
