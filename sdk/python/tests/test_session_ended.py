"""Inside a live run, the run's session ended while the script ran, not by the script (someone ended it from the bench
page, or the lab ended it at its longest time or idle limit): the run stops at once, says why, tells QuPai
(``held`` 0, ``session_ended``) and ends nothing on its way out."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import pytest
from fake_instrucore import FakeInstruCore
from test_live_run import SINCE, points, run_lab

from qupai_lab import LabError, RunSessionEnded
from qupai_lab import runs as _runs


def test_a_run_whose_session_someone_else_ended_stops_at_its_next_write(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            gen = m.role("dg822_pro_usb")
            gen.write("set_output", channel=1, on=True)
            fake.end_by_other("bench")  # the member released it from the bench page
            try:
                gen.write("set_output", channel=1, on=False)
            except Exception:  # noqa: BLE001 - a script that catches what a call raises still stops
                pytest.fail("the run carried on")
    ended = e.value
    assert ended.code == 1 and ended.error_code == "SESSION_ENDED"
    assert "The run's session on signal-bench, held since 2026-11-21 10:00:00 UTC, was ended by someone other than " \
           "this run (from the bench page, or by the lab's admin)" in ended.message
    assert "(BENCH_NOT_HELD)" in ended.message and ended.message.endswith("The run stops.")
    assert ended.detail["refused"]["code"] == "BENCH_NOT_HELD" and ended.detail["machine"] == "signal-bench"
    assert not m.held
    # Its block's end ended nothing: the session was not the run's to end any more.
    assert fake.calls("POST", "/v1/machines/bench/sessions/end") == []
    out = capsys.readouterr()
    found = points(out.out)
    assert found[:-1] == [("held", 1), ("held_since", SINCE), ("calls", 1), ("calls", 2), ("held", 0)]
    assert found[-1][0] == "session_ended" and abs(found[-1][1] - time.time()) < 60
    assert ended.message in out.err


def test_nothing_more_reaches_the_lab_once_the_run_stopped(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RunSessionEnded):  # and the block, that ran on, leaves with it
        with lab.machine("signal-bench") as m:
            gen = m.role("dg822_pro_usb")
            fake.end_by_other("bench")
            with pytest.raises(RunSessionEnded):
                gen.write("set_output", channel=1, on=True)
            seen = len(fake.seen)
            for call in (lambda: gen.read("get_output", channel=1),
                         lambda: gen.write("set_output", channel=1, on=False), lab.machines):
                try:
                    call()
                except BaseException as again:  # noqa: BLE001 - even a script that catches everything
                    assert isinstance(again, RunSessionEnded)
                else:
                    pytest.fail("a call went through")
            assert len(fake.seen) == seen  # none of them reached the lab
    assert fake.calls("POST", "/v1/machines/bench/sessions/end") == []


def test_someone_else_holding_it_now_is_named(fake: FakeInstruCore, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            fake.end_by_other("bench", holder="Bob")
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert "(BENCH_HELD); Bob holds signal-bench now (since 2026-11-21 11:00:00 UTC). The run stops." in e.value.message
    assert fake.sessions["bench"]["client_name"] == "Bob"  # never ended by the run's block


def test_the_session_at_its_longest_time_or_idle_limit_is_said_so(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RunSessionEnded) as longest:
        with lab.machine("signal-bench") as m:
            m._held_at -= 1800  # the fake's sessions last at most 1800 s
            fake.end_by_other("bench")
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert "reached its longest time (30 minutes) and the lab ended it" in longest.value.message
    fake.ended.clear()
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RunSessionEnded) as idle:
        with lab.machine("signal-bench") as m:
            lab._active_at -= 900  # their idle limit is 900 s
            fake.end_by_other("bench")
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert "was ended by the lab after 900 s without a call (its idle limit)" in idle.value.message


def test_a_refusal_while_the_run_still_holds_the_machine_is_the_labs_own(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with lab.machine("signal-bench") as m:
        fake.respond("POST", "/v1/instruments/dg822_pro_usb/write/set_output", 409,
                     {"error": {"code": "SESSION_REQUIRED", "message": "not this one", "detail": {}}})
        with pytest.raises(LabError) as e:
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
        assert e.value.code == "SESSION_REQUIRED" and m.held
        m.role("dg822_pro_usb").write("set_output", channel=1, on=True)  # the run goes on
    assert len(fake.calls("POST", "/v1/machines/bench/sessions/end")) == 1


def test_outside_a_run_a_refused_write_is_only_refused(lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        fake.sessions.clear()
        with pytest.raises(LabError) as e:
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert e.value.code == "SESSION_REQUIRED"


def test_a_copy_a_runs_script_holds_tells_qupai_nothing(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    fake.polls_until_offer = 2
    with lab.machine("signal-bench-sim") as m:
        assert m.held and m.kind == "simulated"
    assert [k for k, _ in points(capsys.readouterr().out)] == []  # the run's held, queue_position: its machine's


def test_the_manifest_says_why_the_run_failed(fake: FakeInstruCore, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    lab.run_folder = _runs.RunFolder(tmp_path / "exp", "job_7", live=True)
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            fake.end_by_other("bench")
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    lab.run_folder.ending(lab._calls, stopped=False)
    manifest = json.loads((tmp_path / "exp" / "runs" / "job_7" / "manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["error"] == e.value.message and manifest["session_ended"]
    [held] = manifest["machines"]
    assert held["ended"] == e.value.message and held["released_at"]


def _post(address: str, path: str) -> dict:
    req = urllib.request.Request(f"http://{address}{path}", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_a_moku_call_the_lab_refuses_stops_the_run_too(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            address = m.moku("moku")["ip"]
            assert _post(address, "/api/moku/claim_ownership")["success"] is True
            fake.end_by_other("bench")
            refused = _post(address, "/api/slot1/spectrumanalyzer/set_span")  # the package raises its own
            assert refused["success"] is False and refused["code"] == "BENCH_NOT_HELD"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:  # the stand-in stops the script in its main thread
                time.sleep(0.01)
            pytest.fail("the run carried on")
    assert "was ended by someone other than this run" in e.value.message and "(BENCH_NOT_HELD)" in e.value.message
    assert fake.calls("POST", "/v1/machines/bench/sessions/end") == []
    assert ("held", 0) in points(capsys.readouterr().out)


def test_after_the_end_the_moku_stand_in_refuses_without_the_lab(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    with pytest.raises(RunSessionEnded):
        with lab.machine("signal-bench") as m:
            address = m.moku("moku")["ip"]
            fake.end_by_other("bench")
            with pytest.raises(RunSessionEnded):
                m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
            seen = len(fake.seen)
            refused = _post(address, "/api/slot1/spectrumanalyzer/get_data")
            assert refused["success"] is False and refused["code"] == "SESSION_ENDED"
            assert "The run stops." in refused["messages"][0]
            assert _post(address, "/api/moku/relinquish_ownership")["success"] is True  # nothing to hand back
            assert len(fake.seen) == seen
