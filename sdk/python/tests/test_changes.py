from __future__ import annotations

import json
from pathlib import Path

import pytest
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


def test_the_running_scripts_blob_is_gits(tmp_path) -> None:
    import subprocess
    import sys

    script = tmp_path / "run.py"
    script.write_text("from qupai_lab import config\nprint(config.running_script_blob())\n")
    got = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True).stdout.strip()
    want = subprocess.run(["git", "hash-object", str(script)], capture_output=True, text=True, check=True).stdout.strip()
    assert got == want


# ---- declared changes: a live run on a lab with no simulated copy ----

DECLARED = [
    {"role": "coil", "op": "set_current", "quantity": "current_a", "min": 0.0, "max": 1.5, "fixed": {"channel": 1}},
    {"role": "moku", "op": "vendor.begin_control"},
    {"role": "scope", "op": "set_timebase", "ranges": {"scale_s": [5e-6, 5e-3], "offset_s": [0, 1e-3]}, "fixed": {}},
]


def test_declared_changes_are_the_cards_changes_json(tmp_path: Path) -> None:
    from qupai_lab import declare_changes

    folder = tmp_path / "experiments" / "2026-11-26-coil"
    folder.mkdir(parents=True)
    out = declare_changes(folder, DECLARED)
    assert out["declared"] is True and out["written_at"].endswith("Z")
    assert out["changes"] == [
        {"role": "coil", "op": "set_current", "quantity": "current_a", "min": 0.0, "max": 1.5, "fixed": {"channel": 1}},
        {"role": "moku", "op": "vendor.begin_control", "fixed": {}},
        {"role": "scope", "op": "set_timebase", "ranges": {"scale_s": [5e-6, 5e-3], "offset_s": [0, 1e-3]},
         "fixed": {}},
    ]
    assert json.loads((folder / "changes.json").read_text()) == out
    # the same shape a rehearsal writes: role, op, fixed, and quantity/min/max or ranges
    rehearsed = group([w("coil", "set_current", channel=1, current_a=a) for a in (0.0, 1.5)])[0]
    assert set(out["changes"][0]) == set(rehearsed) - {"calls"}


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"role": "coil", "op": "set_current", "args": {"channel": 1}}, "unknown args; the arguments it keeps go in fixed"),
        ({"role": "", "op": "set_current"}, "role is the instrument's role"),
        ({"role": "coil", "op": "set current"}, "op is the write op's name"),
        ({"role": "coil", "op": "set_current", "quantity": "current_a", "min": 2, "max": 1}, "min <= max"),
        ({"role": "coil", "op": "set_current", "quantity": "current_a", "min": 0}, "min and max"),
        ({"role": "coil", "op": "set_current", "quantity": "current_a", "min": 0, "max": float("inf")}, "finite"),
        ({"role": "coil", "op": "set_current", "quantity": "current_a", "min": 0, "max": 1, "ranges": {"x": [0, 1]}},
         "not both"),
        ({"role": "coil", "op": "set_current", "quantity": "current_a", "min": 0, "max": 1,
          "fixed": {"current_a": 1}}, "current_a both moves and is fixed"),
        ({"role": "scope", "op": "set_timebase", "ranges": {"scale_s": [1]}}, "ranges.scale_s is [min, max]"),
        ({"role": "coil", "op": "set_current", "fixed": [1]}, "fixed is an object"),
        ({"role": "coil", "op": "set_current", "calls": -1}, "calls is a whole number"),
        ("coil.set_current", "a change is an object"),
    ],
)
def test_a_declared_change_the_card_would_not_take_is_refused(tmp_path: Path, change: object, says: str) -> None:
    from qupai_lab import LabError, declare_changes

    with pytest.raises(LabError) as e:
        declare_changes(tmp_path, [change])
    assert e.value.code == "INVALID_CHANGE" and says in e.value.message
    assert not (tmp_path / "changes.json").exists()


def test_declaring_needs_the_experiment_and_at_most_the_cards_changes(tmp_path: Path) -> None:
    from qupai_lab import LabError, declare_changes

    with pytest.raises(LabError) as e:
        declare_changes(tmp_path / "nowhere", DECLARED)
    assert e.value.code == "NOT_FOUND"
    with pytest.raises(LabError) as e:
        declare_changes(tmp_path, [{"role": "coil", "op": f"op_{i}"} for i in range(33)])
    assert e.value.code == "INVALID_CHANGE" and "at most 32 changes" in e.value.message
    assert declare_changes(tmp_path, [])["changes"] == []  # a run that writes nothing
