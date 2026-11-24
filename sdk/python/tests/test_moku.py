"""``m.moku(device)``: the official moku package's keyword arguments for a Moku of the machine in the block, whose
address names that machine (the live one, or the simulated copy held). These need no ``moku`` installed;
test_moku_official.py drives the package itself through them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError, __version__
from qupai_lab import lab as lab_module
from qupai_lab import runs as _runs
from qupai_lab.lab import moku_address


ROUTE = "/v1/moku/moku-bench-1/moku_go"
NOT_PLAIN = ["", "../moku", "moku/api", "moku go", "moku?x=1", "moku#x", "moku.local", "moku%2F", "_moku", "-moku",
             "m" * 65, "mokü", None, 7]


@pytest.mark.parametrize(
    ("url", "ip"),
    [
        ("http://door.example/lab/lab-sim", f"door.example/lab/lab-sim{ROUTE}"),
        ("http://door.example/lab/lab-sim/", f"door.example/lab/lab-sim{ROUTE}"),
        ("http://door.example/lab/lab-sim//", f"door.example/lab/lab-sim{ROUTE}"),
        ("http://127.0.0.1:59220", f"127.0.0.1:59220{ROUTE}"),
        ("http://127.0.0.1:59220/", f"127.0.0.1:59220{ROUTE}"),
        ("http://[::1]:8080/lab/lab-sim", f"[::1]:8080/lab/lab-sim{ROUTE}"),
    ],
)
def test_the_address_is_the_labs_without_its_scheme_and_the_machines_moku(url: str, ip: str) -> None:
    # The package builds http://{ip}/api/<group>/<operation>: <url>/v1/moku/<machine>/<device>/api/… is InstruCore's
    # endpoint.
    assert moku_address(url, "moku-bench-1", "moku_go") == ip


@pytest.mark.parametrize("plain", ["moku", "moku_pro", "moku-go-2", "Moku1", "m" * 64])
def test_plain_machine_and_device_ids_are_taken_as_given(plain: str) -> None:
    assert moku_address("http://door.example/lab", plain, "moku_go").endswith(f"/v1/moku/{plain}/moku_go")
    assert moku_address("http://door.example/lab", "moku-bench-1", plain).endswith(f"/v1/moku/moku-bench-1/{plain}")


@pytest.mark.parametrize("device", NOT_PLAIN)
def test_a_device_id_that_is_not_a_plain_identifier_is_refused(device: object) -> None:
    with pytest.raises(LabError) as e:
        moku_address("http://door.example/lab/lab-sim", "moku-bench-1", device)
    assert e.value.code == "INVALID_DEVICE"


@pytest.mark.parametrize("machine", NOT_PLAIN)
def test_a_machine_id_that_is_not_a_plain_identifier_is_refused(machine: object) -> None:
    with pytest.raises(LabError) as e:
        moku_address("http://door.example/lab/lab-sim", machine, "moku_go")
    assert e.value.code == "INVALID_MACHINE"


def test_an_https_lab_is_refused_with_why() -> None:
    with pytest.raises(LabError) as e:
        moku_address("https://door.example/lab/lab-sim", "moku-bench-1", "moku_go")
    assert e.value.code == "HTTP_ONLY"
    assert "speaks http only" in e.value.message and "https://door.example/lab/lab-sim" in e.value.message


@pytest.mark.parametrize("url", ["door.example/lab/lab-sim", "ftp://door.example/lab", "http:///lab/lab-sim",
                                 "http://door.example/lab?x=1", "http://door.example/lab#x"])
def test_an_address_that_is_not_plain_http_is_refused(url: str) -> None:
    with pytest.raises(LabError) as e:
        moku_address(url, "moku-bench-1", "moku_go")
    assert e.value.code == "HTTP_ONLY"


def test_the_arguments_for_a_held_copy(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        before = len(fake.seen)
        args = m.moku("moku_go")
        assert len(fake.seen) == before  # builds arguments only: nothing is called
    host = fake.url.removeprefix("http://")
    headers = args.pop("session_headers")
    # One response hook: it reads the writes the lab lists in each answer (instrucore.writes) for changes.json.
    hooks = args.pop("session_hooks")
    assert list(hooks) == ["response"] and len(hooks["response"]) == 1 and callable(hooks["response"][0])
    # The copy the pool gave, by its public id as lab.machines() lists it.
    assert args == {"ip": f"{host}/v1/moku/signal-bench-sim-1/moku_go", "manage_bitstreams": False}
    # The key and the script header this Lab sends on its own calls; no Accept: the package also fetches files.
    assert set(headers) == {"Authorization", "User-Agent", "X-InstruCore-Script"}
    assert headers["Authorization"] == "Bearer qpk_test"
    assert headers["X-InstruCore-Script"] == "experiments/x/run.py@abc1234"
    assert headers["User-Agent"].startswith(f"qupai-lab/{__version__} moku")
    # Nothing forces the Moku: force_connect is the package's own default.
    assert "force_connect" not in args


def test_the_arguments_keep_the_labs_path_prefix_and_drop_a_trailing_slash() -> None:
    fake = FakeInstruCore(prefix="/lab/lab-test").start()
    try:
        lab = Lab(fake.url + "/", "qpk_test", script_ref="")
        with lab.machine("signal-bench-sim") as m:
            args = m.moku("moku_go")
        assert args["ip"] == fake.url.removeprefix("http://") + "/v1/moku/signal-bench-sim-1/moku_go"
        assert args["ip"].endswith("/lab/lab-test/v1/moku/signal-bench-sim-1/moku_go")
        assert "X-InstruCore-Script" not in args["session_headers"]  # script_ref="" sends none, as on its own calls
    finally:
        fake.stop()


def test_each_call_gives_a_new_headers_dict(lab: Lab) -> None:
    """The package makes the dict its HTTP session's headers and adds its Moku-Client-Key to it: one instrument's
    key must not reach the next one, nor this Lab's own calls."""
    with lab.machine("signal-bench-sim") as m:
        first = m.moku("moku_go")
        first["session_headers"]["Moku-Client-Key"] = "from-the-first-instrument"
        second = m.moku("moku_go")
    assert first["session_headers"] is not second["session_headers"]
    assert "Moku-Client-Key" not in second["session_headers"]
    assert "Moku-Client-Key" not in lab._headers(body=False)


def test_the_address_names_the_copy_actually_held(lab: Lab, fake: FakeInstruCore) -> None:
    fake.sessions["signal_bench_sim_1"] = {"session_id": "s_other"}  # someone else holds the first copy
    with lab.machine("signal-bench-sim") as m:
        assert m.public_id == "signal-bench-sim-2"
        args = m.moku("moku_go")
    assert args["ip"].endswith("/v1/moku/signal-bench-sim-2/moku_go")


def test_a_live_machine_someone_else_holds_gives_arguments_too(lab: Lab, fake: FakeInstruCore) -> None:
    # As with m.role(): nothing is held, and the lab decides what each call may do.
    with lab.machine("signal-bench") as m:
        assert not m.held
        args = m.moku("moku_go")
    assert args["ip"].endswith("/v1/moku/signal-bench/moku_go")


def test_outside_a_block_it_says_so(lab: Lab) -> None:
    with pytest.raises(LabError) as e:
        lab.machine("signal-bench-sim").moku("moku_go")
    assert e.value.code == "NOT_HELD"


def test_an_https_lab_is_refused_at_the_call(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        lab.url = "https://door.example/lab/lab-sim"  # as an https QUPAI_LAB_URL would be
        with pytest.raises(LabError) as e:
            m.moku("moku_go")
        lab.url = fake.url
    assert e.value.code == "HTTP_ONLY"


def test_the_run_notes_the_device_and_the_packages_version(fake: FakeInstruCore, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_module, "_installed", lambda package: "4.3.0.1" if package == "moku" else None)
    folder = _runs.RunFolder(tmp_path / "experiment", "r1", live=False)
    lab = Lab(fake.url, "qpk_test", script_ref="", run_folder=folder)
    with lab.machine("signal-bench-sim") as m:
        args = m.moku("moku_go")
        m.moku("moku_go")  # noted once
        m.moku("moku_pro")
    assert args["session_headers"]["User-Agent"] == f"qupai-lab/{__version__} moku/4.3.0.1"
    manifest = json.loads((folder.path / "manifest.json").read_text())
    assert manifest["official_sdk"] == [
        {"package": "moku", "version": "4.3.0.1", "machine": "signal-bench-sim-1", "device": "moku_go"},
        {"package": "moku", "version": "4.3.0.1", "machine": "signal-bench-sim-1", "device": "moku_pro"},
    ]
    assert manifest["versions"]["moku"] == "4.3.0.1"
    # The lab records the package's calls; the Lab SDK's record and changes have none of them.
    out = lab.changes("changes.json")
    assert [x["device"] for x in out["official_sdk"]] == ["moku_go", "moku_pro"]
    assert out["changes"] == []


def test_without_the_package_the_version_is_unknown(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_module, "_installed", lambda package: None)
    with lab.machine("signal-bench-sim") as m:
        args = m.moku("moku_go")
    assert args["session_headers"]["User-Agent"] == f"qupai-lab/{__version__} moku"
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
