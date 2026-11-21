from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fake_instrucore import FakeInstruCore

from qupai_lab import __version__
from qupai_lab.cli import main


def test_machines_prints_each_machine_with_its_instruments_by_role(
    fake: FakeInstruCore, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("QUPAI_LAB_URL", fake.url)
    monkeypatch.setenv("QUPAI_LAB_KEY", "qpk_test")
    fake.sessions["signal_bench_sim_2"] = {"session_id": "s_9", "machine": "signal_bench_sim_2"}
    assert main(["machines"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"Lab {fake.url}"
    assert out[1].split() == ["MACHINE", "KIND", "STATE", "INSTRUMENTS", "BY", "ROLE"]
    assert out[2].split() == ["signal-bench", "live", "free", "dg822_pro_usb,", "tbs1104_usb"]
    assert out[3].split() == ["signal-bench-sim", "pool", "1", "of", "2", "free"]
    assert out[4].split()[:3] == ["signal-bench-sim-1", "simulated", "free"]
    assert "dg822_pro_usb=signal_bench_sim_1_dg822_pro_usb" in out[4]
    assert out[5].split()[:3] == ["signal-bench-sim-2", "simulated", "in_session"]
    assert "X-InstruCore-Script" not in fake.seen[0].headers  # the command is not a script


def test_machines_json(fake: FakeInstruCore, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("QUPAI_LAB_URL", fake.url)
    monkeypatch.setenv("QUPAI_LAB_KEY", "qpk_test")
    assert main(["machines", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data["machines"]] == ["signal-bench", "signal-bench-sim-1", "signal-bench-sim-2"]


def test_machines_reports_a_refusal(fake: FakeInstruCore, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("QUPAI_LAB_URL", fake.url)
    monkeypatch.setenv("QUPAI_LAB_KEY", "qpk_bad")
    fake.respond("GET", "/v1/machines", 401, {"type": "urn:qupai:problem:invalid-key", "title": "Invalid key",
                                              "status": 401, "detail": "The key is not valid"},
                 content_type="application/problem+json")
    assert main(["machines"]) == 1
    assert capsys.readouterr().err.strip() == "qupai-lab: INVALID_KEY: The key is not valid"


def test_machines_without_settings(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["machines"]) == 1
    assert "NOT_CONFIGURED" in capsys.readouterr().err


def test_version() -> None:
    done = subprocess.run([sys.executable, "-m", "qupai_lab", "--version"], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == f"qupai-lab {__version__}"
