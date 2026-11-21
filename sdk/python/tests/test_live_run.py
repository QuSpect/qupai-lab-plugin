"""Inside a live run (``QUPAI_LAB_RUN``, ``QUPAI_LAB_RUN_MACHINE``): the run's live machine is held through its
queue, any other live machine is not, and the run's points are ``QUPAI_METRIC`` lines."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError
from qupai_lab import lab as lab_module

RUN_KEY = "qrk_run_7"
# The QuPai Runtime's rule (qupai_runtime/platform/jobs.py, metric_points): a line that starts with this marker and
# a JSON object; {"key": k, "value": v} is one point of k, a name of these characters, v a finite number.
MARKER = "QUPAI_METRIC "
METRIC_KEY = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")


def points(out: str) -> list[tuple[str, float]]:
    found = []
    for line in out.splitlines():
        if not line.startswith(MARKER):
            continue
        record = json.loads(line[len(MARKER):])
        assert set(record) == {"key", "value"} and METRIC_KEY.match(record["key"])
        assert isinstance(record["value"], (int, float)) and not isinstance(record["value"], bool)
        found.append((record["key"], record["value"]))
    return found


def run_lab(fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine: str) -> Lab:
    monkeypatch.setenv("QUPAI_LAB_RUN", "job_7")
    monkeypatch.setenv("QUPAI_LAB_RUN_MACHINE", machine)
    fake.run_keys.add(RUN_KEY)
    lab = Lab(fake.url, RUN_KEY, record=tmp_path / "calls.jsonl", script_ref="experiments/x/run.py@abc1234")
    lab.poll_s = 0
    lab._sleep = lambda s: None
    return lab


def test_the_runs_live_machine_is_held_through_its_queue(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    fake.polls_until_offer, fake.start_position = 3, 3
    with lab.machine("signal-bench") as m:
        assert (m.id, m.public_id, m.kind, m.held) == ("bench", "signal-bench", "live", True)
        assert m.session["machine"] == "bench" and "bench" in fake.sessions
        assert m.role("dg822_pro_usb").write("set_waveform", channel=1, frequency_hz=1000) is None
    order = [(s.method, s.path) for s in fake.seen if s.method != "GET" or "session-requests" in s.path]
    assert order == [
        ("POST", "/v1/machines/signal-bench/sessions"),
        ("GET", "/v1/session-requests/q_0001"),
        ("GET", "/v1/session-requests/q_0001"),
        ("GET", "/v1/session-requests/q_0001"),
        ("POST", "/v1/machines/signal-bench/sessions"),  # within the offer: starts the session
        ("POST", "/v1/instruments/dg822_pro_usb/write/set_waveform"),
        ("POST", "/v1/machines/bench/sessions/end"),
    ]
    assert [s.body for s in fake.calls("POST", "/v1/machines/signal-bench/sessions")] == [
        {"purpose": "run job_7", "queue": True}] * 2  # no simulated flag
    assert fake.calls("POST", "/v1/machines/bench/sessions/end")[0].body == {
        "reason": "qupai-lab: the script's block ended"}
    assert fake.sessions == {} and not m.held
    out = capsys.readouterr()
    assert points(out.out) == [("queue_position", 3), ("queue_position", 2), ("queue_position", 1), ("held", 1),
                               ("calls", 1), ("held", 0)]
    assert "queued for signal-bench at position 3; waiting for its turn" in out.err


def test_the_runs_machine_by_its_internal_id(fake: FakeInstruCore, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "bench")
    with lab.machine("signal-bench") as m:  # 201 at once: the machine is free and nobody waits
        assert m.held and m.id == "bench"
    assert len(fake.calls("POST", "/v1/machines/bench/sessions/end")) == 1


def test_a_run_that_took_its_members_own_session_leaves_it_held(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    fake.taken = True
    lab = run_lab(fake, tmp_path, monkeypatch, "bench")
    with lab.machine("signal-bench") as m:
        assert m.held and m.taken
    assert fake.calls("POST", "/v1/machines/bench/sessions/end") == []
    assert "stays held" in capsys.readouterr().err


def test_a_live_machine_that_is_not_the_runs_is_not_held(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "other-bench")
    with lab.machine("signal-bench") as m:
        assert (m.id, m.kind, m.held) == ("bench", "live", False)
        with pytest.raises(LabError) as e:
            m.role("dg822_pro_usb").write("set_waveform", channel=1, frequency_hz=1000)
    assert e.value.code == "APPROVAL_REQUIRED"
    assert fake.calls("POST", "/v1/machines/") == []  # neither a session nor its end
    assert points(capsys.readouterr().out) == [("calls", 1)]


def test_a_runs_session_is_released_when_the_block_raises(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RuntimeError):
        with lab.machine("signal-bench"):
            raise RuntimeError("the script failed")
    end = fake.calls("POST", "/v1/machines/bench/sessions/end")
    assert [s.body["reason"] for s in end] == ["qupai-lab: the script's block raised"]
    assert fake.sessions == {}
    assert points(capsys.readouterr().out) == [("held", 1), ("held", 0)]


def test_a_refused_run_session_is_passed_through(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    fake.run_keys.clear()  # the lab does not take this key as the run's
    with pytest.raises(LabError) as e:
        with lab.machine("signal-bench"):
            pytest.fail("the block must not run")
    assert e.value.code == "RUN_REQUIRED"
    assert points(capsys.readouterr().out) == []


def test_the_calls_point_is_said_at_most_every_few_seconds_and_at_the_end(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(lab_module, "CALLS_EVERY_S", 3600.0)
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with lab.machine("signal-bench") as m:
        gen = m.role("dg822_pro_usb")
        for f in (100, 300, 1000, 3000):
            gen.write("set_waveform", channel=1, frequency_hz=f)
        gen.read("get_waveform", channel=1)
    assert points(capsys.readouterr().out) == [("held", 1), ("calls", 1), ("calls", 5), ("held", 0)]


def test_outside_a_run_nothing_is_printed_on_stdout(lab: Lab, fake: FakeInstruCore, capsys) -> None:
    fake.polls_until_offer = 3
    with lab.machine("signal-bench-sim") as m:
        m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert capsys.readouterr().out == ""
