from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import lines
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError


def test_machines_lists_kinds_pools_and_roles(lab: Lab, fake: FakeInstruCore) -> None:
    machines = {m["id"]: m for m in lab.machines()}
    assert set(machines) == {"signal-bench", "signal-bench-sim-1", "signal-bench-sim-2"}
    bench = machines["signal-bench"]
    assert (bench["internal_id"], bench["kind"], bench["state"], bench["pool"]) == ("bench", "live", "free", None)
    assert bench["roles"] == {"dg822_pro_usb": "dg822_pro_usb", "tbs1104_usb": "tbs1104_usb"}
    copy = machines["signal-bench-sim-1"]
    assert (copy["internal_id"], copy["kind"], copy["pool"]) == ("signal_bench_sim_1", "simulated", "signal-bench-sim")
    assert copy["roles"]["dg822_pro_usb"] == "signal_bench_sim_1_dg822_pro_usb"
    seen = fake.calls("GET", "/v1/machines")[0]
    assert seen.headers["Authorization"] == "Bearer qpk_test"
    assert seen.headers["X-InstruCore-Script"] == "experiments/x/run.py@abc1234"


def test_pool_held_for_the_block_with_201_and_released(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        assert (m.id, m.public_id, m.kind, m.held) == ("signal_bench_sim_1", "signal-bench-sim-1", "simulated", True)
        assert m.session["session_id"] == "s_0001"
        assert "signal_bench_sim_1" in fake.sessions
    start = fake.calls("POST", "/v1/machines/signal-bench-sim/sessions")
    assert [s.body for s in start] == [{"purpose": "experiments/x/run.py@abc1234", "queue": True, "simulated": True}]
    end = fake.calls("POST", "/v1/machines/signal_bench_sim_1/sessions/end")
    assert len(end) == 1 and end[0].body["reason"].startswith("qupai-lab")
    assert fake.sessions == {} and not m.held


def test_202_waits_until_offered_then_asks_again(lab: Lab, fake: FakeInstruCore, capsys) -> None:
    fake.polls_until_offer = 3
    with lab.machine("signal-bench-sim", purpose="frequency response") as m:
        assert m.id == "signal_bench_sim_1" and m.held
    order = [(s.method, s.path) for s in fake.seen]
    assert order == [
        ("GET", "/v1/machines"),
        ("POST", "/v1/machines/signal-bench-sim/sessions"),
        ("GET", "/v1/session-requests/q_0001"),
        ("GET", "/v1/session-requests/q_0001"),
        ("GET", "/v1/session-requests/q_0001"),
        ("POST", "/v1/machines/signal-bench-sim/sessions"),
        ("POST", "/v1/machines/signal_bench_sim_1/sessions/end"),
    ]
    assert all(s.body["purpose"] == "frequency response" for s in fake.calls("POST", "/v1/machines/signal-bench-sim"))
    assert "queued for signal-bench-sim at position 2" in capsys.readouterr().err
    recorded = [(x["method"], x["path"], x["status"]) for x in lines(lab.recorder.path)]
    assert recorded == [
        ("GET", "/v1/machines", 200),
        ("POST", "/v1/machines/signal-bench-sim/sessions", 202),
        ("GET", "/v1/session-requests/q_0001", 200),  # only the look that saw the offer
        ("POST", "/v1/machines/signal-bench-sim/sessions", 201),
        ("POST", "/v1/machines/signal_bench_sim_1/sessions/end", 200),
    ]


def test_waiting_past_wait_s_withdraws_the_request(lab: Lab, fake: FakeInstruCore) -> None:
    fake.polls_until_offer = 100
    with pytest.raises(LabError) as e:
        with lab.machine("signal-bench-sim", wait_s=0):
            pytest.fail("the block must not run")
    assert e.value.code == "WAIT_TIMEOUT"
    assert [s.path for s in fake.calls("DELETE")] == ["/v1/session-requests/q_0001"]
    assert fake.requests["q_0001"]["status"] == "cancelled"


def test_released_when_the_block_raises(lab: Lab, fake: FakeInstruCore) -> None:
    with pytest.raises(RuntimeError):
        with lab.machine("signal-bench-sim") as m:
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
            raise RuntimeError("the script failed")
    assert len(fake.calls("POST", "/v1/machines/signal_bench_sim_1/sessions/end")) == 1
    assert fake.sessions == {}


def test_a_session_that_already_ended_is_not_an_error(lab: Lab, fake: FakeInstruCore, capsys) -> None:
    with lab.machine("signal-bench-sim"):
        fake.sessions.clear()  # ended by the lab (idle, longest time)
    assert "had already ended" in capsys.readouterr().err


def test_a_copy_by_its_own_id_is_held(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim-2") as m:
        assert m.id == "signal_bench_sim_2" and m.held
    assert fake.calls("POST", "/v1/machines/signal-bench-sim-2/sessions")[0].body["simulated"] is True


def test_live_machine_is_not_held_and_the_lab_refuses(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench") as m:
        assert (m.id, m.kind, m.held) == ("bench", "live", False)
        gen = m.role("dg822_pro_usb")
        assert gen.id == "dg822_pro_usb"
        assert gen.read("get_waveform", channel=1) == {"op": "get_waveform", "args": {"channel": 1}}
        with pytest.raises(LabError) as e:
            gen.write("set_waveform", channel=1, frequency_hz=1000)
    assert e.value.code == "APPROVAL_REQUIRED"
    assert e.value.message == "the holder has not allowed this write"  # passed through as the lab said it
    assert fake.calls("POST", "/v1/machines/") == []  # neither a session nor its end


def test_run_required_is_passed_through(lab: Lab) -> None:
    with pytest.raises(LabError) as e:
        lab.request("POST", "/v1/machines/bench/sessions", {"queue": True})
    assert (e.value.code, e.value.status) == ("RUN_REQUIRED", 403)
    assert e.value.message.startswith("a live machine is held only for a run the member approved")
    assert str(e.value) == f"RUN_REQUIRED: {e.value.message}"


def test_unknown_machine_and_role(lab: Lab) -> None:
    with pytest.raises(LabError) as e:
        with lab.machine("nowhere"):
            pass
    assert e.value.code == "NOT_FOUND" and "signal-bench-sim" in e.value.message
    with lab.machine("signal-bench-sim") as m:
        with pytest.raises(LabError) as e:
            m.role("laser")
    assert e.value.code == "UNKNOWN_ROLE" and e.value.detail["roles"] == ["dg822_pro_usb", "tbs1104_usb"]
    with pytest.raises(LabError) as e:
        lab.machine("signal-bench").role("dg822_pro_usb")
    assert e.value.code == "NOT_HELD"


def test_read_write_check_payloads_and_record(lab: Lab, fake: FakeInstruCore, tmp_path: Path) -> None:
    with lab.machine("signal-bench-sim") as m:
        gen, scope = m.role("dg822_pro_usb"), m.role("tbs1104_usb")
        assert gen.check("set_waveform", channel=1, frequency_hz=100) == {"ok": True, "kind": "write"}
        assert gen.write("set_waveform", channel=1, shape="sine", frequency_hz=100, amplitude_v=1.0) is None
        trace = scope.read("acquire", channel=1, points=2000)
        saved = lab.save(tmp_path / "data/raw/trace_100.json", trace, frequency_hz=100)
        settings = gen.read("get_waveform", channel=1)
        assert gen.reference() == {"schema": "mhs-reference/0", "instrument": "signal_bench_sim_1_dg822_pro_usb"}
    assert trace == {"channel": 1, "samples": [0.0, 0.5, 1.0, 0.5, 0.0]}
    assert saved == {"file": str(tmp_path / "data/raw/trace_100.json"), "frequency_hz": 100}
    assert json.loads((tmp_path / "data/raw/trace_100.json").read_text()) == trace

    ops = {s.path: s.body for s in fake.calls("POST", "/v1/instruments/")}
    assert ops["/v1/instruments/signal_bench_sim_1_dg822_pro_usb/check/set_waveform"] == {
        "channel": 1, "frequency_hz": 100}
    assert ops["/v1/instruments/signal_bench_sim_1_dg822_pro_usb/write/set_waveform"] == {
        "channel": 1, "shape": "sine", "frequency_hz": 100, "amplitude_v": 1.0}
    assert ops["/v1/instruments/signal_bench_sim_1_tbs1104_usb/read/acquire"] == {"channel": 1, "points": 2000}

    record = {x["path"]: x for x in lines(lab.recorder.path)}
    write = record["/v1/instruments/signal_bench_sim_1_dg822_pro_usb/write/set_waveform"]
    assert {k: write[k] for k in ("method", "machine", "role", "op", "kind", "ok", "status", "code")} == {
        "method": "POST", "machine": "signal-bench-sim-1", "role": "dg822_pro_usb", "op": "set_waveform",
        "kind": "write", "ok": True, "status": 200, "code": None}
    assert write["args"]["frequency_hz"] == 100 and write["result"] is None and write["req_id"].startswith("r")
    assert write["time"].endswith("Z")
    acquire = record["/v1/instruments/signal_bench_sim_1_tbs1104_usb/read/acquire"]
    assert acquire["file"] == str(tmp_path / "data/raw/trace_100.json") and "result" not in acquire
    get = record["/v1/instruments/signal_bench_sim_1_dg822_pro_usb/read/get_waveform"]
    assert get["result"] == settings and "file" not in get  # not saved: the result is in the line


def test_a_refused_write_is_a_record_line_and_not_a_change(lab: Lab, fake: FakeInstruCore, tmp_path: Path) -> None:
    fake.respond("POST", "/v1/instruments/signal_bench_sim_1_dg822_pro_usb/write/set_waveform", 403, {
        "error": {"code": "LIMIT_EXCEEDED", "message": "amplitude_v 3 is outside the agent limit [0, 2]",
                  "detail": {"limit": "instruments.x.limits.agent.amplitude_v", "value": 3.0, "allowed": [0, 2],
                             "tier": "agent"}},
        "req_id": "601c4a90b7cf7687"})
    with lab.machine("signal-bench-sim") as m:
        with pytest.raises(LabError) as e:
            m.role("dg822_pro_usb").write("set_waveform", channel=1, amplitude_v=3.0)
    assert (e.value.code, e.value.status, e.value.detail["allowed"]) == ("LIMIT_EXCEEDED", 403, [0, 2])
    line = [x for x in lines(lab.recorder.path) if x.get("op") == "set_waveform"][0]
    assert (line["ok"], line["code"], line["status"], line["req_id"]) == (False, "LIMIT_EXCEEDED", 403,
                                                                          "601c4a90b7cf7687")
    assert lab.changes(tmp_path / "changes.json")["changes"] == []


def test_save_and_table(lab: Lab, tmp_path: Path) -> None:
    rows = [{"frequency_hz": 100, **lab.save(tmp_path / "a/b/t.json", [1, 2])},
            {"frequency_hz": 300, "file": "x.json", "peak": {"v": 1}}]
    assert lab.table(tmp_path / "out/response.csv", rows) == {"file": str(tmp_path / "out/response.csv"), "rows": 2}
    text = (tmp_path / "out/response.csv").read_text().splitlines()
    assert text[0] == "frequency_hz,file,peak"
    assert text[2] == '300,x.json,"{""v"": 1}"'


def test_unreachable(tmp_path: Path) -> None:
    lab = Lab("http://127.0.0.1:9", "k", script_ref="")
    with pytest.raises(LabError) as e:
        lab.machines()
    assert e.value.code == "UNREACHABLE" and e.value.status == 0
