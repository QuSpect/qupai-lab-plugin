from __future__ import annotations

import json
from pathlib import Path

from fake_instrucore import FakeInstruCore

from qupai_lab import Lab
from qupai_lab.changes import Write, group


def w(role: str, op: str, **args: object) -> Write:
    return Write(role, op, dict(args), "signal-bench-sim-1")


def test_one_moving_argument_is_a_range() -> None:
    writes = [w("gen", "set_waveform", channel=1, shape="sine", frequency_hz=f, amplitude_v=1.0)
              for f in [100, 300, 1e3, 3e3, 1e5]]
    assert group(writes) == [{
        "role": "gen", "op": "set_waveform", "calls": 5, "quantity": "frequency_hz", "min": 100, "max": 1e5,
        "fixed": {"channel": 1, "shape": "sine", "amplitude_v": 1.0}}]


def test_several_moving_arguments_are_a_box() -> None:
    writes = [w("scope", "set_timebase", scale_s=s, offset_s=o, mode="main") for s, o in
              [(5e-3, 0.0), (5e-6, 1e-6), (5e-4, -1e-6)]]
    assert group(writes) == [{
        "role": "scope", "op": "set_timebase", "calls": 3,
        "ranges": {"scale_s": [5e-6, 5e-3], "offset_s": [-1e-6, 1e-6]}, "fixed": {"mode": "main"}}]


def test_nothing_moving_is_fixed_and_others_split() -> None:
    writes = [
        w("gen", "set_output", channel=1, on=True),
        w("gen", "set_output", channel=1, on=True),
        w("gen", "set_waveform", channel=1, shape="sine", frequency_hz=10),
        w("gen", "set_waveform", channel=2, shape="sine", frequency_hz=20),  # another channel: never a range
        w("gen", "set_waveform", channel=1, shape="square", frequency_hz=30),  # another shape
        w("gen", "set_waveform", channel=1, shape="sine", frequency_hz=40),
        w("gen", "set_output", channel=1, on=False),
    ]
    assert group(writes) == [
        {"role": "gen", "op": "set_output", "calls": 2, "fixed": {"channel": 1, "on": True}},
        {"role": "gen", "op": "set_waveform", "calls": 2, "quantity": "frequency_hz", "min": 10, "max": 40,
         "fixed": {"channel": 1, "shape": "sine"}},
        {"role": "gen", "op": "set_waveform", "calls": 1, "fixed": {"channel": 2, "shape": "sine", "frequency_hz": 20}},
        {"role": "gen", "op": "set_waveform", "calls": 1,
         "fixed": {"channel": 1, "shape": "square", "frequency_hz": 30}},
        {"role": "gen", "op": "set_output", "calls": 1, "fixed": {"channel": 1, "on": False}},
    ]


def test_booleans_never_range() -> None:
    assert group([w("gen", "set_output", on=True), w("gen", "set_output", on=False)])[0]["fixed"] == {"on": True}


def test_lab_changes_writes_the_file(lab: Lab, fake: FakeInstruCore, tmp_path: Path) -> None:
    with lab.machine("signal-bench-sim") as m:
        gen, scope = m.role("dg822_pro_usb"), m.role("tbs1104_usb")
        for f in [100, 1e3, 1e5]:
            gen.write("set_waveform", channel=1, shape="sine", frequency_hz=f, amplitude_v=1.0)
            scope.write("set_timebase", scale_s=0.5 / f, offset_s=0.0)
    out = lab.changes(tmp_path / "changes.json")
    assert json.loads((tmp_path / "changes.json").read_text()) == out
    assert out["script"] == "experiments/x/run.py@abc1234" and out["machines"] == ["signal-bench-sim-1"]
    assert out["changes"] == [
        {"role": "dg822_pro_usb", "op": "set_waveform", "calls": 3, "quantity": "frequency_hz", "min": 100,
         "max": 1e5, "fixed": {"channel": 1, "shape": "sine", "amplitude_v": 1.0}},
        {"role": "tbs1104_usb", "op": "set_timebase", "calls": 3, "quantity": "scale_s", "min": 0.5 / 1e5,
         "max": 0.5 / 100, "fixed": {"offset_s": 0.0}},
    ]
