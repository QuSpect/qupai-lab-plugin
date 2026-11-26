"""``Lab.from_qupai``: a lab worked directly with the person's own qupai sign-in, at the platform or through a lab
workstation's door (a Unix socket)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError, direct
from qupai_lab.cli import main

KEY = "qpk_person_secret_0123456789"
SIM = {"lab": "sim", "name": "Simulated lab", "listing": "lab-sim", "role": "operate", "via": "account"}
MAGNETIC = {"lab": "magnetic", "name": {"en": "Magnetic", "zh": "磁"}, "listing": "lab-magnetic", "role": "operate",
            "via": "workstation"}
unix_only = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix sockets on this system")


@pytest.fixture
def platform() -> Iterator[FakeInstruCore]:
    """The platform and its gateway on one address: ``/v1/me/lab-access`` (canned), and the lab's REST door at
    ``/rest/lab-sim/``."""
    server = FakeInstruCore(prefix="/rest/lab-sim").start()
    yield server
    server.stop()


def root(fake: FakeInstruCore) -> str:
    return fake.url[: -len(fake.prefix)] if fake.prefix else fake.url


def access(fake: FakeInstruCore, labs: list[dict[str, Any]], ask: bool = False, times: int = 50) -> None:
    fake.respond("GET", "/v1/me/lab-access", 200, {"labs": labs, "ask": ask}, times=times)


def sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: str, **extra: Any) -> Path:
    path = tmp_path / "qupai" / "credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"server": server, "kind": "browser", **({"api_key": KEY} | extra)}))
    monkeypatch.setenv(direct.CREDENTIALS_ENV, str(path))
    return path


@pytest.fixture
def door_path() -> Iterator[str]:
    """A short folder for a Unix socket (a socket's path has at most 104 bytes on macOS)."""
    folder = tempfile.mkdtemp(prefix="qlab-", dir="/tmp" if os.path.isdir("/tmp") else None)
    yield os.path.join(folder, "lab.sock")
    shutil.rmtree(folder, ignore_errors=True)


# ── the sign-in ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("platform_name", "env", "expected"), [
    ("darwin", {"HOME": "/Users/ada"}, "/Users/ada/Library/Application Support/qupai/credentials.json"),
    ("linux", {"HOME": "/home/ada", "XDG_CONFIG_HOME": "/home/ada/.cfg"}, "/home/ada/.cfg/qupai/credentials.json"),
    ("linux", {"HOME": "/home/ada"}, "/home/ada/.config/qupai/credentials.json"),
    ("linux", {"HOME": "/home/ada", "XDG_CONFIG_HOME": "relative"}, None),
    ("linux", {}, None),
], ids=["macos", "linux-xdg", "linux-home", "linux-relative-xdg", "no-home"])
def test_the_signin_is_where_qupai_login_keeps_it(monkeypatch: pytest.MonkeyPatch, platform_name: str,
                                                  env: dict[str, str], expected: str | None) -> None:
    monkeypatch.delenv(direct.CREDENTIALS_ENV, raising=False)
    for name in ("HOME", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(direct.sys, "platform", platform_name)
    found = direct.credentials_path()
    assert (str(found) if found else None) == expected


def test_the_signin_on_windows_is_in_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(direct.CREDENTIALS_ENV, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(direct.sys, "platform", "win32")
    assert direct.credentials_path() == tmp_path / "qupai" / "credentials.json"


def test_qupai_credentials_names_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(direct.CREDENTIALS_ENV, str(tmp_path / "mine.json"))
    assert direct.credentials_path() == tmp_path / "mine.json"


def test_no_signin_says_to_run_qupai_login(tmp_path: Path) -> None:
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert e.value.code == "NOT_SIGNED_IN"
    assert "qupai login" in e.value.message and str(tmp_path / "no-signin" / "credentials.json") in e.value.message


def test_a_device_signin_has_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"server": "https://qupai.example", "access_token": "eyJ-token", "refresh_token": "r"}))
    monkeypatch.setenv(direct.CREDENTIALS_ENV, str(path))
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert e.value.code == "NOT_SIGNED_IN"
    assert "device sign-in" in e.value.message and "qupai login" in e.value.message
    assert "eyJ-token" not in str(e.value)


# ── at the platform ─────────────────────────────────────────────────────────────────────────────


def test_from_qupai_finds_the_lab_and_speaks_rest_with_the_persons_key(
    platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(tmp_path, monkeypatch, root(platform) + "/")
    access(platform, [MAGNETIC, SIM])
    lab = Lab.from_qupai("sim", runs=False)
    assert lab.url == f"{root(platform)}/rest/lab-sim" and lab.unix_socket is None
    assert KEY not in repr(lab)
    lab.poll_s = 0
    with lab.machine("signal-bench-sim") as m:
        m.role("dg822_pro_usb").write("set_waveform", channel=1, frequency_hz=1000)
    first = platform.seen[0]
    assert (first.method, first.full_path) == ("GET", "/v1/me/lab-access")
    assert first.headers["Authorization"] == f"Bearer {KEY}"
    rest = platform.seen[1:]
    assert rest and all(s.full_path.startswith("/rest/lab-sim/v1/") for s in rest)
    assert all(s.headers["Authorization"] == f"Bearer {KEY}" for s in rest)
    assert [s.path for s in rest if s.method == "POST"][1] == "/v1/instruments/signal_bench_sim_1_dg822_pro_usb/write/set_waveform"


def test_the_listing_names_the_lab_too(platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    access(platform, [SIM])
    assert Lab.from_qupai("lab-sim", runs=False).url.endswith("/rest/lab-sim")


def test_a_run_folder_keeps_the_listing_not_the_address(
    platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    access(platform, [SIM])
    monkeypatch.chdir(tmp_path)
    lab = Lab.from_qupai("sim", runs=True)
    assert lab.run_folder is not None and lab.run_folder.lab == "lab-sim"


def test_qupai_gateway_names_where_the_rest_door_is(
    platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeInstruCore().start()  # the platform's API only; the gateway is the other address
    try:
        sign_in(tmp_path, monkeypatch, server.url)
        access(server, [SIM])
        monkeypatch.setenv(direct.GATEWAY_ENV, root(platform) + "/")
        lab = Lab.from_qupai("sim", runs=False)
        assert lab.url == f"{root(platform)}/rest/lab-sim"
        assert [m["id"] for m in lab.machines()][0] == "signal-bench"
        assert [s.path for s in server.seen] == ["/v1/me/lab-access"]
    finally:
        server.stop()


def test_a_lab_not_listed_names_the_labs_the_person_may_work_directly(
    platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    access(platform, [MAGNETIC, SIM], ask=True)
    with pytest.raises(LabError) as e:
        Lab.from_qupai("csmot")
    err = e.value
    assert err.code == "NO_DIRECT_ACCESS"
    assert "'csmot' is not a lab you may work directly from this machine" in err.message
    assert "magnetic (Magnetic): operate, through this workstation" in err.message
    assert "sim (Simulated lab): operate, granted to your account" in err.message
    assert "ask is the way" in err.message and 'qupai ask -lab <lab>' in err.message
    assert err.detail == {"lab": "csmot", "labs": ["magnetic", "sim"], "ask": True, "socket": None}
    assert KEY not in str(err)
    assert [s.path for s in platform.seen] == ["/v1/me/lab-access"]  # nothing went to any lab


def test_no_lab_at_all_says_ask_is_the_way(platform: FakeInstruCore, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    access(platform, [], ask=True)
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert e.value.code == "NO_DIRECT_ACCESS"
    assert "you may work no lab directly from this machine (direct access is a grant QuPai staff make)" in e.value.message
    assert "ask is the way" in e.value.message


def test_a_refused_key_says_to_sign_in_again(platform: FakeInstruCore, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    path = sign_in(tmp_path, monkeypatch, root(platform))
    platform.respond("GET", "/v1/me/lab-access", 401,
                     {"type": "urn:qupai:problem:invalid-key", "title": "Invalid key", "detail": "This key is revoked"},
                     content_type="application/problem+json")
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert (e.value.code, e.value.status) == ("INVALID_KEY", 401)
    assert e.value.message.startswith("This key is revoked") and str(path) in e.value.message
    assert "qupai login" in e.value.message and KEY not in str(e.value)


def test_a_platform_without_direct_access(platform: FakeInstruCore, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    platform.respond("GET", "/v1/me/lab-access", 404, {"error": "not found"})
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert e.value.code == "NOT_OFFERED" and "qupai ask -lab" in e.value.message


def test_an_unreachable_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sign_in(tmp_path, monkeypatch, f"http://127.0.0.1:{port}")
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert e.value.code == "UNREACHABLE" and f"127.0.0.1:{port}" in e.value.message


def test_the_command_line_lists_a_direct_labs_machines(
    platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    access(platform, [SIM])
    assert main(["machines", "--lab", "sim"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"Lab {root(platform)}/rest/lab-sim"
    assert out[2].split()[:3] == ["signal-bench", "live", "free"]
    assert "X-InstruCore-Script" not in platform.seen[-1].headers
    assert main(["machines", "--lab", "csmot"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("qupai-lab: NO_DIRECT_ACCESS: 'csmot' is not a lab you may work directly") and KEY not in err


# ── on a lab workstation: through the door ──────────────────────────────────────────────────────


@unix_only
def test_on_a_workstation_every_call_goes_through_the_door(
    platform: FakeInstruCore, door_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform.serve_unix(door_path)
    sign_in(tmp_path, monkeypatch, "https://qupai.example")  # an address nothing answers: only the door carries it
    monkeypatch.setenv(direct.SOCKET_ENV, door_path)
    access(platform, [{**SIM, "via": "workstation"}])
    lab = Lab.from_qupai("sim", runs=False)
    assert (lab.url, lab.unix_socket) == ("https://qupai.example/rest/lab-sim", door_path)
    lab.poll_s = 0
    with lab.machine("signal-bench-sim") as m:
        m.role("dg822_pro_usb").write("set_waveform", channel=1, frequency_hz=1000)
    assert [s.full_path for s in platform.seen][:2] == ["/v1/me/lab-access", "/rest/lab-sim/v1/machines"]
    assert all(s.headers["Host"] == "qupai.example" for s in platform.seen)
    assert all(s.headers["Authorization"] == f"Bearer {KEY}" for s in platform.seen)
    # No workstation header comes from here: the door adds them, with a credential no seat can read.
    assert not any(k.lower().startswith("x-qupai-workstation") for s in platform.seen for k in s.headers)


@unix_only
def test_the_doors_default_socket_is_used_when_it_exists(
    platform: FakeInstruCore, door_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform.serve_unix(door_path)
    monkeypatch.setattr(direct, "WORKSTATION_SOCKET", Path(door_path))
    sign_in(tmp_path, monkeypatch, "https://qupai.example")
    access(platform, [MAGNETIC])
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert e.value.code == "NO_DIRECT_ACCESS"
    assert "from this workstation: magnetic (Magnetic): operate, through this workstation" in e.value.message
    assert e.value.detail["socket"] == door_path


@unix_only
def test_a_door_that_does_not_answer(door_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in(tmp_path, monkeypatch, "https://qupai.example")
    monkeypatch.setenv(direct.SOCKET_ENV, door_path)  # no one listens there
    with pytest.raises(LabError) as e:
        Lab.from_qupai("sim")
    assert e.value.code == "UNREACHABLE"
    assert f"https://qupai.example, through the workstation's door at {door_path}" in e.value.message


@unix_only
def test_the_moku_stand_in_goes_through_the_door(
    platform: FakeInstruCore, door_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform.serve_unix(door_path)
    sign_in(tmp_path, monkeypatch, "https://qupai.example")
    monkeypatch.setenv(direct.SOCKET_ENV, door_path)
    access(platform, [SIM])
    lab = Lab.from_qupai("sim", runs=False)
    lab.poll_s = 0
    with lab.machine("signal-bench-sim") as m:
        args = m.moku("moku_go")
        req = urllib.request.Request(f"http://{args['ip']}/api/moku/describe")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                answer = json.loads(r.read())
        except urllib.error.HTTPError as e:  # pragma: no cover - the test fails below with the lab's answer
            answer = json.loads(e.read())
    assert answer["success"] is True
    moku = [s for s in platform.seen if "/v1/moku/" in s.full_path]
    assert [s.full_path for s in moku] == ["/rest/lab-sim/v1/moku/signal-bench-sim-1/moku_go/api/moku/describe"]
    assert moku[0].headers["Host"] == "qupai.example" and moku[0].headers["Authorization"] == f"Bearer {KEY}"


# ── a live machine, worked directly ─────────────────────────────────────────────────────────────


def test_a_direct_lab_holds_a_live_machine_for_the_block(
    platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    access(platform, [SIM])
    platform.direct_keys.add(KEY)
    lab = Lab.from_qupai("sim", runs=False)
    lab.script_ref = "scans/angle.py@abc1234"
    with lab.machine("signal-bench") as m:
        assert m.held and m.kind == "live" and "bench" in platform.sessions
        m.role("dg822_pro_usb").write("set_waveform", channel=1, frequency_hz=40)
    assert "bench" not in platform.sessions  # released at the block's end
    starts = [s for s in platform.seen if s.method == "POST" and s.path.endswith("/sessions")]
    assert [s.body for s in starts] == [{"purpose": "scans/angle.py@abc1234", "queue": True}]  # not simulated
    ends = [s for s in platform.seen if s.path.endswith("/sessions/end")]
    assert ends[0].body == {"reason": "qupai-lab: the script's block ended"}


def test_a_session_the_person_already_held_stays_held(
    platform: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    sign_in(tmp_path, monkeypatch, root(platform))
    access(platform, [SIM])
    platform.direct_keys.add(KEY)
    lab = Lab.from_qupai("sim", runs=False)
    lab.request("POST", "/v1/machines/signal-bench/sessions", {"purpose": "the conversation", "queue": True})
    held = platform.sessions["bench"]["session_id"]
    with lab.machine("signal-bench") as m:
        assert m.held and m.session["session_id"] == held
    assert platform.sessions["bench"]["session_id"] == held  # the person's own: theirs to end
    assert not [s for s in platform.seen if s.path.endswith("/sessions/end")]
    assert "signal-bench stays held: it was your session before the block" in capsys.readouterr().err


def test_a_sandbox_lab_still_does_not_hold_a_live_machine(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench") as m:
        assert not m.held
    assert not [s for s in fake.seen if s.method == "POST"]
