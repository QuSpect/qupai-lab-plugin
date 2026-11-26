"""Inside a live run whose session ended under it (someone ended it from the bench page, or the lab at its idle limit):
the script's clean-up (its ``finally``, an ``except``, a ``with`` block's exit) puts back what the run changed, in a
session the run takes for that when the machine is free and nobody waits; what it cannot put back, and what the run
set and did not put back, the manifest's ``restore`` and ``error`` say (Magnetic, 2026-11-26: the input left on DC /
400 mVpp twice)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_instrucore import FakeInstruCore
from test_live_run import run_lab
from test_session_ended import _post

from qupai_lab import Lab, LabError, RunSessionEnded
from qupai_lab import runs as _runs

GEN = "dg822_pro_usb"
WRITE = f"/v1/instruments/{GEN}/write/set_output"
SESSIONS = "/v1/machines/bench/sessions"


def asked(fake: FakeInstruCore) -> list[dict]:
    """Every session the run asked for, by either id of the machine."""
    return [s.body for s in fake.calls("POST", "/v1/machines/") if s.path.endswith("/sessions")]


def live_lab(fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    lab = run_lab(fake, tmp_path, monkeypatch, "signal-bench")
    lab.run_folder = _runs.RunFolder(tmp_path / "exp", "job_7", live=True)
    return lab


def manifest(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "exp" / "runs" / "job_7" / "manifest.json").read_text())


def test_the_clean_up_puts_back_what_the_run_set_in_a_session_of_its_own(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    lab = live_lab(fake, tmp_path, monkeypatch)
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            gen = m.role(GEN)
            gen.write("set_output", channel=1, on=True)
            gen.write("set_waveform", channel=2, shape="sine", frequency_hz=40)
            try:
                fake.end_by_other("bench")  # the member released it from the bench page
                gen.write("set_output", channel=1, on=True)  # the scan's next write: the run stops
            finally:
                gen.write("set_output", channel=1, on=False)  # the script's own clean-up
                assert gen.read("get_output", channel=1)["op"] == "get_output"  # and its check of it
    taken = asked(fake)
    assert taken == [{"purpose": "run job_7", "queue": True},
                     {"purpose": "run job_7: putting back its settings", "queue": False, "max_s": 120.0}]
    [ended] = [s.body for s in fake.calls("POST", f"{SESSIONS}/end")]
    assert ended == {"reason": "qupai-lab: the run's settings are put back"}
    assert fake.sessions == {}  # the block's end ended the session it took
    put = manifest(tmp_path)["restore"]
    sid = put["session"]
    assert put["machine"] == "signal-bench" and sid.startswith("s_") and put["why_not"] is None
    [after] = put["after_end"]
    assert (after["role"], after["op"], after["args"], after["put_back"]) == (GEN, "set_output",
                                                                            {"channel": 1, "on": False}, True)
    # channel 1's output is put back; channel 2's waveform the clean-up never wrote again
    assert put["left_set"] == [{"role": GEN, "op": "set_waveform",
                                "args": {"channel": 2, "shape": "sine", "frequency_hz": 40}}]
    message = e.value.message
    assert "was ended by someone other than this run" in message and "The run stops." in message
    assert f"Its clean-up put back 1 setting on signal-bench in session {sid}, which it then ended." in message
    assert message.endswith("Still set on signal-bench by this run, as far as it knows: set_waveform of "
                            f"{GEN} (channel 2) (the manifest's restore.left_set): check them before the next run.")
    assert manifest(tmp_path)["error"] == message
    assert f"putting back the run's settings on signal-bench in session {sid}" in capsys.readouterr().err


def test_a_finally_after_the_scripts_own_error_puts_back_too(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production's order: the reads failed after the end (the Moku lost its control), the script raised its own
    error, and the first write of its ``finally`` was the call that found the session gone."""
    lab = live_lab(fake, tmp_path, monkeypatch)
    restored = False
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            gen = m.role(GEN)
            gen.write("set_output", channel=1, on=True)
            try:
                fake.end_by_other("bench")
                raise RuntimeError("acquire failed 10 times")
            finally:
                try:
                    gen.write("set_output", channel=1, on=False)
                    restored = True
                except LabError:
                    pass
    assert restored
    assert isinstance(e.value.__cause__, RuntimeError)  # the block leaves with the reason, from the script's error
    writes = [s.body for s in fake.calls("POST", WRITE)]
    assert writes == [{"channel": 1, "on": True}, {"channel": 1, "on": False}, {"channel": 1, "on": False}]
    put = manifest(tmp_path)["restore"]
    assert put["session"] and put["left_set"] == [] and [a["put_back"] for a in put["after_end"]] == [True]
    assert "Still set" not in e.value.message


def test_someone_holding_it_now_gets_nothing_and_the_manifest_says_so(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = live_lab(fake, tmp_path, monkeypatch)
    refused: list[LabError] = []
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            gen = m.role(GEN)
            gen.write("set_output", channel=1, on=True)
            try:
                fake.end_by_other("bench", holder="Bob")
                gen.write("set_output", channel=1, on=True)
            finally:
                for on in (False, False):  # a clean-up that goes on after a refusal: each is listed
                    try:
                        gen.write("set_output", channel=1, on=on)
                    except LabError as x:
                        refused.append(x)
    assert [x.code for x in refused] == ["NOT_PUT_BACK"] * 2
    assert "did not reach signal-bench: the run's session there had ended, and Bob holds signal-bench now" in \
        refused[0].message
    assert len(asked(fake)) == 1  # no session asked for: Bob has it
    assert len(fake.calls("POST", WRITE)) == 2  # the first write and the one that found the end
    assert fake.sessions["bench"]["client_name"] == "Bob"
    put = manifest(tmp_path)["restore"]
    assert put["session"] is None and put["why_not"] == "Bob holds signal-bench now"
    assert [(a["put_back"], a["why"]) for a in put["after_end"]] == [(False, "Bob holds signal-bench now")] * 2
    assert put["left_set"] == [{"role": GEN, "op": "set_output", "args": {"channel": 1, "on": True}}]
    assert "2 changes of its clean-up did not reach signal-bench (Bob holds signal-bench now)." in e.value.message
    assert "set_output of dg822_pro_usb (channel 1)" in e.value.message


def test_nobody_jumps_the_queue_to_put_settings_back(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = live_lab(fake, tmp_path, monkeypatch)
    with pytest.raises(RunSessionEnded):
        with lab.machine("signal-bench") as m:
            gen = m.role(GEN)
            gen.write("set_output", channel=1, on=True)
            try:
                fake.end_by_other("bench")
                fake.queues["bench"] = [{"request_id": "q_9", "position": 1}]
                gen.write("set_output", channel=1, on=True)
            finally:
                with pytest.raises(LabError, match="someone is waiting for signal-bench"):
                    gen.write("set_output", channel=1, on=False)
    assert len(asked(fake)) == 1
    assert manifest(tmp_path)["restore"]["why_not"] == "someone is waiting for signal-bench"


def test_after_its_longest_time_the_run_takes_nothing_more(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = live_lab(fake, tmp_path, monkeypatch)
    with pytest.raises(RunSessionEnded) as e:
        with lab.machine("signal-bench") as m:
            gen = m.role(GEN)
            gen.write("set_output", channel=1, on=True)
            m._held_at -= 1800  # the fake's sessions last at most 1800 s
            try:
                fake.end_by_other("bench")
                gen.write("set_output", channel=1, on=True)
            finally:
                with pytest.raises(LabError) as x:
                    gen.write("set_output", channel=1, on=False)
    assert x.value.code == "NOT_PUT_BACK" and "the run's longest time is over" in x.value.message
    assert len(asked(fake)) == 1 and fake.calls("GET", "/v1/machines/bench")
    assert "reached its longest time" in e.value.message and "(the run's longest time is over)" in e.value.message


def test_a_put_back_the_lab_refuses_is_the_labs_answer(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = live_lab(fake, tmp_path, monkeypatch)
    with pytest.raises(RunSessionEnded):
        with lab.machine("signal-bench") as m:
            gen = m.role(GEN)
            gen.write("set_output", channel=1, on=True)
            try:
                fake.end_by_other("bench")
                gen.write("set_output", channel=1, on=True)
            finally:
                fake.respond("POST", WRITE, 403, {"error": {"code": "APPROVAL_REQUIRED",
                                                            "message": "not on the run's card", "detail": {}}})
                with pytest.raises(LabError) as x:
                    gen.write("set_output", channel=1, on=False)
    assert x.value.code == "APPROVAL_REQUIRED"
    assert len(fake.calls("POST", f"{SESSIONS}/end")) == 1  # the session it took is ended all the same
    [after] = manifest(tmp_path)["restore"]["after_end"]
    assert after["put_back"] is False and after["why"] == "APPROVAL_REQUIRED: not on the run's card"


def test_a_script_that_carries_on_after_the_end_still_reaches_nothing(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a script that stops puts back: one that swallows the end and goes on with its scan takes no session."""
    lab = live_lab(fake, tmp_path, monkeypatch)
    with pytest.raises(RunSessionEnded):
        with lab.machine("signal-bench") as m:
            gen = m.role(GEN)
            fake.end_by_other("bench")
            for f in (100, 200):
                try:
                    gen.write("set_waveform", channel=1, frequency_hz=f)
                except BaseException:  # noqa: BLE001 - a script that swallows everything
                    pass
            seen = len(fake.seen)
            with pytest.raises(RunSessionEnded):
                gen.write("set_waveform", channel=1, frequency_hz=300)
            assert len(fake.seen) == seen
    assert len(asked(fake)) == 1


def test_after_the_block_ended_nothing_is_taken(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean-up outside the machine's block finds it ended: put settings back inside the block."""
    lab = live_lab(fake, tmp_path, monkeypatch)
    with pytest.raises(LabError) as x:
        try:
            with lab.machine("signal-bench") as m:
                gen = m.role(GEN)
                gen.write("set_output", channel=1, on=True)
                fake.end_by_other("bench")
                gen.write("set_output", channel=1, on=True)
        finally:
            gen.write("set_output", channel=1, on=False)
    assert x.value.code == "NOT_PUT_BACK" and "the script's block on signal-bench had ended" in x.value.message
    assert len(asked(fake)) == 1


def test_the_moku_packages_changes_after_the_end_are_listed(
    fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = live_lab(fake, tmp_path, monkeypatch)
    with pytest.raises(RunSessionEnded):
        with lab.machine("signal-bench") as m:
            address = m.moku("moku")["ip"]
            fake.end_by_other("bench")
            with pytest.raises(RunSessionEnded):
                m.role(GEN).write("set_output", channel=1, on=True)
            for path in ("/api/slot1/spectrumanalyzer/set_frontend", "/api/slot1/spectrumanalyzer/get_data",
                         "/api/moku/relinquish_ownership"):
                _post(address, path)
    after = manifest(tmp_path)["restore"]["after_end"]
    assert [(a["device"], a["call"], a["put_back"]) for a in after] == [
        ("moku", "POST slot1/spectrumanalyzer/set_frontend", False)]


def test_outside_a_run_nothing_changes(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        gen = m.role(GEN)
        try:
            raise RuntimeError("the script's own error")
        except RuntimeError:
            gen.write("set_output", channel=1, on=False)  # an ordinary write in an except
    assert len(fake.calls("POST", "/v1/machines/signal-bench-sim/sessions")) == 1
