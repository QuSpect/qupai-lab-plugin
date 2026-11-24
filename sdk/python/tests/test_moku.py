"""``m.moku(device)``: the official moku package's keyword arguments for a Moku of the machine in the block. The
package talks to a local stand-in (``qupai_lab.moku_forward``) whose calls go to the lab's endpoint for that machine
(the live one, or the simulated copy held). These need no ``moku`` installed; test_moku_official.py drives the package
itself through them."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError, __version__
from qupai_lab import lab as lab_module
from qupai_lab import runs as _runs
from qupai_lab.lab import moku_target

ROUTE = "/v1/moku/moku-bench-1/moku_go"
NOT_PLAIN = ["", "../moku", "moku/api", "moku go", "moku?x=1", "moku#x", "moku.local", "moku%2F", "_moku", "-moku",
             "m" * 65, "mokü", None, 7]


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("http://door.example/lab/lab-sim", f"http://door.example/lab/lab-sim{ROUTE}"),
        ("http://door.example/lab/lab-sim/", f"http://door.example/lab/lab-sim{ROUTE}"),
        ("http://door.example/lab/lab-sim//", f"http://door.example/lab/lab-sim{ROUTE}"),
        ("http://127.0.0.1:59220", f"http://127.0.0.1:59220{ROUTE}"),
        ("http://[::1]:8080/lab/lab-sim", f"http://[::1]:8080/lab/lab-sim{ROUTE}"),
        # the forwarder makes the call, so an https lab works too (the package itself speaks http only)
        ("https://door.example/lab/lab-sim", f"https://door.example/lab/lab-sim{ROUTE}"),
    ],
)
def test_the_target_is_the_labs_endpoint_for_the_machines_moku(url: str, target: str) -> None:
    assert moku_target(url, "moku-bench-1", "moku_go") == target


@pytest.mark.parametrize("plain", ["moku", "moku_pro", "moku-go-2", "Moku1", "m" * 64])
def test_plain_machine_and_device_ids_are_taken_as_given(plain: str) -> None:
    assert moku_target("http://door.example/lab", plain, "moku_go").endswith(f"/v1/moku/{plain}/moku_go")
    assert moku_target("http://door.example/lab", "moku-bench-1", plain).endswith(f"/v1/moku/moku-bench-1/{plain}")


@pytest.mark.parametrize("device", NOT_PLAIN)
def test_a_device_id_that_is_not_a_plain_identifier_is_refused(device: object) -> None:
    with pytest.raises(LabError) as e:
        moku_target("http://door.example/lab/lab-sim", "moku-bench-1", device)
    assert e.value.code == "INVALID_DEVICE"


@pytest.mark.parametrize("machine", NOT_PLAIN)
def test_a_machine_id_that_is_not_a_plain_identifier_is_refused(machine: object) -> None:
    with pytest.raises(LabError) as e:
        moku_target("http://door.example/lab/lab-sim", machine, "moku_go")
    assert e.value.code == "INVALID_MACHINE"


@pytest.mark.parametrize("url", ["door.example/lab/lab-sim", "ftp://door.example/lab", "http:///lab/lab-sim",
                                 "http://door.example/lab?x=1", "http://door.example/lab#x"])
def test_an_address_that_is_not_plain_http_or_https_is_refused(url: str) -> None:
    with pytest.raises(LabError) as e:
        moku_target(url, "moku-bench-1", "moku_go")
    assert e.value.code == "INVALID_URL"


def _get(address: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://{address}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_arguments_for_a_held_copy(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        before = len(fake.seen)
        args = m.moku("moku_go")
        assert len(fake.seen) == before  # builds arguments and the local stand-in only: nothing is called
        forwarder = m._forwarders["moku_go"]
        # The package reaches the stand-in directly, whatever proxy the environment names.
        assert args == {"ip": forwarder.address, "manage_bitstreams": False, "session_trust_env": False,
                        "session_proxies": {}}
        assert args["ip"].startswith("127.0.0.1:")
        # Its calls go to the copy the pool gave, by its public id as lab.machines() lists it, with this Lab's key and
        # script header; no Accept: the package also fetches files.
        assert forwarder.target == f"{fake.url}/v1/moku/signal-bench-sim-1/moku_go"
        assert set(forwarder.headers) == {"Authorization", "User-Agent", "X-InstruCore-Script"}
        assert forwarder.headers["Authorization"] == "Bearer qpk_test"
        assert forwarder.headers["X-InstruCore-Script"] == "experiments/x/run.py@abc1234"
        assert forwarder.headers["User-Agent"].startswith(f"qupai-lab/{__version__} moku")
        # Nothing forces the Moku: force_connect is the package's own default.
        assert "force_connect" not in args
    # The block's end closes the stand-in.
    with pytest.raises(OSError):
        _get(args["ip"], "/api/moku/describe")


def test_the_stand_in_passes_calls_on_with_the_labs_key(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        ip = m.moku("moku_go")["ip"]
        # The package's own client key passes on; a key of its own does not replace the Lab's.
        status, body = _get(ip, "/api/moku/describe", {"Moku-Client-Key": "k1", "Authorization": "Bearer other"})
        assert status == 200 and body["success"] is True
        seen = fake.seen[-1]
        assert seen.path == "/v1/moku/signal-bench-sim-1/moku_go/api/moku/describe"
        assert seen.headers["Authorization"] == "Bearer qpk_test" and seen.headers["Moku-Client-Key"] == "k1"
        # Only the Moku's API is passed on.
        status, body = _get(ip, "/v1/machines")
        assert status == 404 and body["code"] == "NOT_FOUND"
        assert fake.seen[-1] is seen


def test_a_lab_that_does_not_answer_is_said_in_the_mokus_envelope(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        ip = m.moku("moku_go")["ip"]
        m._forwarders["moku_go"].target = "http://127.0.0.1:9/v1/moku/signal-bench-sim-1/moku_go"  # nothing listens
        status, body = _get(ip, "/api/moku/describe")
    assert status == 200 and body["success"] is False and body["code"] == "LAB_UNREACHABLE"
    assert "nothing was sent to the Moku" in body["messages"][0]


def test_the_target_keeps_the_labs_path_prefix_and_drops_a_trailing_slash() -> None:
    fake = FakeInstruCore(prefix="/lab/lab-test").start()
    try:
        lab = Lab(fake.url + "/", "qpk_test", script_ref="")
        with lab.machine("signal-bench-sim") as m:
            m.moku("moku_go")
            forwarder = m._forwarders["moku_go"]
            assert forwarder.target.endswith("/lab/lab-test/v1/moku/signal-bench-sim-1/moku_go")
            assert "X-InstruCore-Script" not in forwarder.headers  # script_ref="" sends none, as on its own calls
    finally:
        fake.stop()


def test_one_stand_in_per_device(lab: Lab) -> None:
    """Calling m.moku() again for the same device reuses its stand-in; another device has its own. The package's
    client key travels with each request and never reaches this Lab's own calls."""
    with lab.machine("signal-bench-sim") as m:
        first, again, other = m.moku("moku_go"), m.moku("moku_go"), m.moku("moku_pro")
        assert first["ip"] == again["ip"] != other["ip"]
        assert sorted(m._forwarders) == ["moku_go", "moku_pro"]
    assert "Moku-Client-Key" not in lab._headers(body=False)


def test_the_target_names_the_copy_actually_held(lab: Lab, fake: FakeInstruCore) -> None:
    fake.sessions["signal_bench_sim_1"] = {"session_id": "s_other"}  # someone else holds the first copy
    with lab.machine("signal-bench-sim") as m:
        assert m.public_id == "signal-bench-sim-2"
        m.moku("moku_go")
        assert m._forwarders["moku_go"].target.endswith("/v1/moku/signal-bench-sim-2/moku_go")


def test_a_live_machine_someone_else_holds_gives_arguments_too(lab: Lab, fake: FakeInstruCore) -> None:
    # As with m.role(): nothing is held, and the lab decides what each call may do.
    with lab.machine("signal-bench") as m:
        assert not m.held
        m.moku("moku_go")
        assert m._forwarders["moku_go"].target.endswith("/v1/moku/signal-bench/moku_go")


def test_outside_a_block_it_says_so(lab: Lab) -> None:
    with pytest.raises(LabError) as e:
        lab.machine("signal-bench-sim").moku("moku_go")
    assert e.value.code == "NOT_HELD"


def test_an_https_lab_works_through_the_stand_in(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        lab.url = "https://door.example/lab/lab-sim"  # as an https QUPAI_LAB_URL would be
        try:
            m.moku("moku_go")
            assert m._forwarders["moku_go"].target == "https://door.example/lab/lab-sim/v1/moku/signal-bench-sim-1/moku_go"
        finally:
            lab.url = fake.url


def test_the_run_notes_the_device_and_the_packages_version(fake: FakeInstruCore, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_module, "_installed", lambda package: "4.3.0.1" if package == "moku" else None)
    folder = _runs.RunFolder(tmp_path / "experiment", "r1", live=False)
    lab = Lab(fake.url, "qpk_test", script_ref="", run_folder=folder)
    with lab.machine("signal-bench-sim") as m:
        m.moku("moku_go")
        m.moku("moku_go")  # noted once
        m.moku("moku_pro")
        assert m._forwarders["moku_go"].headers["User-Agent"] == f"qupai-lab/{__version__} moku/4.3.0.1"
    manifest = json.loads((folder.path / "manifest.json").read_text())
    assert manifest["official_sdk"] == [
        {"package": "moku", "version": "4.3.0.1", "machine": "signal-bench-sim-1", "device": "moku_go"},
        {"package": "moku", "version": "4.3.0.1", "machine": "signal-bench-sim-1", "device": "moku_pro"},
    ]
    assert manifest["versions"]["moku"] == "4.3.0.1"
    # The lab records the package's calls; nothing was called here, so changes have none of them.
    out = lab.changes("changes.json")
    assert [x["device"] for x in out["official_sdk"]] == ["moku_go", "moku_pro"]
    assert out["changes"] == []


def test_without_the_package_the_version_is_unknown(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_module, "_installed", lambda package: None)
    with lab.machine("signal-bench-sim") as m:
        m.moku("moku_go")
        assert m._forwarders["moku_go"].headers["User-Agent"] == f"qupai-lab/{__version__} moku"
    assert lab._official_used == [{"package": "moku", "version": None, "machine": "signal-bench-sim-1",
                                   "device": "moku_go"}]


def test_a_run_without_the_official_sdk_has_an_empty_list_and_changes_leave_it_out(fake: FakeInstruCore,
                                                                                  tmp_path: Path) -> None:
    folder = _runs.RunFolder(tmp_path / "experiment", "r1", live=False)
    lab = Lab(fake.url, "qpk_test", script_ref="", run_folder=folder)
    with lab.machine("signal-bench-sim") as m:
        m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert json.loads((folder.path / "manifest.json").read_text())["official_sdk"] == []
    assert "official_sdk" not in lab.changes("changes.json")
