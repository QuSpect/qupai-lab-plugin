"""The official moku package (4.3.0.1, MIT) driven through ``m.moku()`` against the fake lab's stand-in for
InstruCore's Moku endpoint, mounted under a path as a QuPai sandbox's door mounts a lab. Skipped without the package:
``pip install -e ".[moku]"`` installs it."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import lines
from fake_instrucore import MOKU_CLIENT_KEY, FakeInstruCore, Seen

from qupai_lab import Lab

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # importing moku warns that it cannot find mokucli, which none of this needs
    instruments = pytest.importorskip("moku.instruments")
    exceptions = pytest.importorskip("moku.exceptions")

PREFIX = "/lab/lab-test"
DEVICE = "moku_go"
COPY = "signal-bench-sim-1"  # the copy the pool gives first, by its public id


@pytest.fixture
def door() -> Iterator[FakeInstruCore]:
    server = FakeInstruCore(prefix=PREFIX).start()
    yield server
    server.stop()


def moku_calls(fake: FakeInstruCore) -> list[Seen]:
    return [s for s in fake.seen if s.path.startswith("/v1/moku/")]


def test_an_oscilloscope_through_the_lab(door: FakeInstruCore, tmp_path: Path) -> None:
    lab = Lab(door.url + "/", "qpk_test", record=tmp_path / "calls.jsonl", script_ref="experiments/x/run.py@abc1234")
    with lab.machine("signal-bench-sim") as m:
        with instruments.Oscilloscope(**m.moku(DEVICE)) as osc:
            data = osc.get_data()
    assert data["ch1"] == [0.0, 0.5, 1.0]

    # http://{ip}/api/… is <QUPAI_LAB_URL>/v1/moku/<machine>/<device>/api/…, with the lab's path prefix and the copy
    # the block holds.
    base = f"{PREFIX}/v1/moku/{COPY}/{DEVICE}/api"
    calls = moku_calls(door)
    assert [(s.method, s.full_path) for s in calls] == [
        ("POST", f"{base}/moku/claim_ownership"),
        ("GET", f"{base}/moku/describe"),
        ("POST", f"{base}/slot1/oscilloscope/get_data"),
        ("POST", f"{base}/moku/relinquish_ownership"),
    ]
    # Every call carries the Lab's own key and script header; the package adds the client key the claim answered.
    for s in calls:
        assert s.headers["Authorization"] == "Bearer qpk_test"
        assert s.headers["X-InstruCore-Script"] == "experiments/x/run.py@abc1234"
        assert s.headers["User-Agent"].startswith("qupai-lab/")
    assert calls[0].headers.get("Moku-Client-Key") is None
    assert all(s.headers["Moku-Client-Key"] == MOKU_CLIENT_KEY for s in calls[1:])
    # Nothing forces: the claim asks with the package's defaults.
    assert calls[0].body == {"force_connect": False, "force_deploy": False, "ignore_busy": False,
                             "persist_state": False}
    # No bitstream is uploaded from the sandbox (manage_bitstreams=False).
    assert not any("bitstream" in s.path for s in door.seen)
    # The Moku's calls fall inside the block's hold.
    order = [s.path for s in door.seen]
    assert order.index("/v1/machines/signal-bench-sim/sessions") < order.index(calls[0].path)
    assert order.index(calls[-1].path) < order.index("/v1/machines/signal_bench_sim_1/sessions/end")
    # The lab records them; the Lab SDK's record has only its own calls.
    assert not any(x["path"].startswith("/v1/moku/") for x in lines(tmp_path / "calls.jsonl"))


def test_a_spectrum_analyzer_in_multi_instrument_mode(door: FakeInstruCore) -> None:
    lab = Lab(door.url, "qpk_test", script_ref="")
    with lab.machine("signal-bench-sim") as m:
        mim = instruments.MultiInstrument(**m.moku("moku_pro"), platform_id=4)
        sa = mim.set_instrument(1, instruments.SpectrumAnalyzer)
        sa.set_span(frequency1=0, frequency2=10e6)
        data = sa.get_data()
        mim.relinquish_ownership()
    assert data["ch2"] == [1.0, 0.5, 0.0]
    calls = moku_calls(door)
    assert all(s.path.startswith(f"/v1/moku/{COPY}/moku_pro/api/") for s in calls)
    assert [(s.method, s.path.split("/api/", 1)[1]) for s in calls] == [
        ("POST", "moku/claim_ownership"),
        ("GET", "moku/describe"),
        ("GET", "moku/platform/4"),
        ("GET", "mim/get_instruments"),
        ("GET", "slot1/spectrumanalyzer"),
        ("POST", "slot1/spectrumanalyzer/set_span"),
        ("POST", "slot1/spectrumanalyzer/get_data"),
        ("POST", "moku/relinquish_ownership"),
    ]
    assert calls[5].body == {"strict": True, "frequency1": 0, "frequency2": 10e6}
    assert "X-InstruCore-Script" not in calls[0].headers  # script_ref="": none, as on the Lab's own calls


def test_the_calls_name_the_copy_actually_held(door: FakeInstruCore) -> None:
    door.sessions["signal_bench_sim_1"] = {"session_id": "s_other"}  # someone else holds the first copy
    lab = Lab(door.url, "qpk_test", script_ref="")
    with lab.machine("signal-bench-sim") as m:
        with instruments.Oscilloscope(**m.moku(DEVICE)) as osc:
            osc.get_data()
    calls = moku_calls(door)
    assert calls and all(s.path.startswith(f"/v1/moku/signal-bench-sim-2/{DEVICE}/api/") for s in calls)


def test_each_instrument_starts_without_the_last_ones_client_key(door: FakeInstruCore) -> None:
    lab = Lab(door.url, "qpk_test", script_ref="")
    with lab.machine("signal-bench-sim") as m:
        with instruments.Oscilloscope(**m.moku(DEVICE)):
            pass
        with instruments.Oscilloscope(**m.moku(DEVICE)):
            pass
    claims = [s for s in moku_calls(door) if s.path.endswith("/moku/claim_ownership")]
    assert len(claims) == 2
    assert all(s.headers.get("Moku-Client-Key") is None for s in claims)


def test_a_refusal_in_the_mokus_own_envelope_reaches_the_script(door: FakeInstruCore) -> None:
    """The package raises the messages of an answer with success false; an HTTP error status reaches it only as
    "Unknown exception. Status code: …"."""
    door.moku_refusal = "this Moku's machine is not held with this key: hold it first"
    lab = Lab(door.url, "qpk_test", script_ref="")
    with lab.machine("signal-bench-sim") as m:
        with pytest.raises(exceptions.MokuException) as e:
            instruments.Oscilloscope(**m.moku(DEVICE))
    assert "is not held with this key" in str(e.value)
    assert door.sessions == {}  # the block still released the copy


def test_a_rehearsals_changes_list_the_writes_the_lab_listed(door: FakeInstruCore, tmp_path: Path) -> None:
    """InstruCore 0.26.2 lists each write it made in its answer (instrucore.writes); the Lab SDK keeps them for
    changes.json under the instrument's role, so a live run's card can approve the package's writes."""
    door.moku_writes_role = "tbs1104_usb"
    lab = Lab(door.url, "qpk_test", script_ref="experiments/x/run.py@abc1234")
    with lab.machine("signal-bench-sim") as m:
        mim = instruments.MultiInstrument(**m.moku("moku_pro"), platform_id=4)
        sa = mim.set_instrument(1, instruments.SpectrumAnalyzer)
        sa.set_span(frequency1=0, frequency2=10e6)
        sa.get_data()
        mim.relinquish_ownership()
    out = lab.changes(tmp_path / "changes.json")
    assert [(c["role"], c["op"], c["calls"], c["fixed"]) for c in out["changes"]] == [
        ("tbs1104_usb", "moku.moku.platform", 1, {"platform_id": 4}),
        ("tbs1104_usb", "moku.slot1.spectrumanalyzer.deploy", 1, {}),
        ("tbs1104_usb", "moku.slot1.spectrumanalyzer.set_span", 1, {"strict": True, "frequency1": 0, "frequency2": 10e6}),
    ]
    assert out["official_sdk"] == [{"package": "moku", "version": "4.3.0.1", "machine": COPY, "device": "moku_pro"}]


def test_a_lab_that_lists_no_writes_is_noted(door: FakeInstruCore, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Before 0.26.2 InstruCore's answers list nothing: the writes are not in changes.json, and it says so."""
    lab = Lab(door.url, "qpk_test", script_ref="")
    with lab.machine("signal-bench-sim") as m:
        mim = instruments.MultiInstrument(**m.moku("moku_pro"), platform_id=4)
        mim.set_instrument(1, instruments.SpectrumAnalyzer).set_span(frequency1=0, frequency2=10e6)
        mim.relinquish_ownership()
    out = lab.changes(tmp_path / "changes.json")
    assert out["changes"] == []
    assert out["official_sdk"][0]["writes_listed"] is False
    assert capsys.readouterr().err.count("lists no writes") == 1
