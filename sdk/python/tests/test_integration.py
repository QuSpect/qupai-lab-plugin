"""Against a real InstruCore with a simulated pool, when QUPAI_LAB_IT_URL and QUPAI_LAB_IT_KEY are set.

For example InstruCore serving examples/instrucore.qupai-sim.yaml or instrucore.sim-lab.yaml, with a read-write key
(docs/design/api.md, First steps); QUPAI_LAB_IT_POOL picks the pool (the first one by default)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qupai_lab import Lab, LabError

URL, KEY = os.environ.get("QUPAI_LAB_IT_URL"), os.environ.get("QUPAI_LAB_IT_KEY")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (URL and KEY), reason="set QUPAI_LAB_IT_URL and QUPAI_LAB_IT_KEY to run"),
]


def test_a_script_on_a_simulated_copy(tmp_path: Path) -> None:
    lab = Lab(str(URL), str(KEY), record=tmp_path / "calls.jsonl", script_ref="tests/test_integration.py@local")
    raw = lab.request("GET", "/v1/machines")
    assert raw["pools"], "the lab has no simulated pool"
    pool = os.environ.get("QUPAI_LAB_IT_POOL") or raw["pools"][0].get("public_id") or raw["pools"][0]["id"]
    assert lab.machines()

    with lab.machine(pool) as m:
        assert m.held and m.kind == "simulated" and m.roles
        held = m.id
        assert m.reference()
        for role in m.roles:
            assert m.role(role).reference()
        gen = next((m.role(r) for r in m.roles if r in ("generator", "dg822_pro_usb")), None)
        if gen is not None:  # the signal bench's generator: rehearse one change and read it back
            assert gen.check("set_waveform", channel=1, shape="sine", frequency_hz=1000, amplitude_v=1.0)["ok"]
            for f in (1000, 2000):
                gen.write("set_waveform", channel=1, shape="sine", frequency_hz=f, amplitude_v=1.0)
            assert gen.read("get_waveform", channel=1)["frequency_hz"] == 2000
            with pytest.raises(LabError) as e:
                gen.write("set_waveform", channel=1, frequency_hz=1e9)
            assert e.value.code in ("LIMIT_EXCEEDED", "INVALID_ARGS")
    after = next(x for x in lab.machines() if x["internal_id"] == held)
    assert after["state"] in ("free", "offered")  # released: reset and given back to the pool
    out = lab.changes(tmp_path / "changes.json")
    if gen is not None:
        assert out["changes"][0]["quantity"] == "frequency_hz"
    assert (tmp_path / "calls.jsonl").read_text().count("\n") >= 4
