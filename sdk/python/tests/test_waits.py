"""Waiting, bounded and said (0.6.2): the platform's 429 (and a 503 that names a Retry-After) waited for and the call
sent again when that is safe, and a block waiting for its machine says what it waits for while it waits.

On a lab workstation, 2026-11-26, QuPai's gateway answered every call 429 ("too many requests from this address",
Retry-After 60) for about 30 s after a burst; a run's script raised and its clean-up failed too. Another run printed
nothing for 8 minutes while it waited, until it was killed."""

from __future__ import annotations

import json
import signal
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from conftest import lines
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError, direct, pace, stop
from qupai_lab import runs as _runs

RATE = {"type": "urn:qupai:problem:rate", "title": "Too many requests", "status": 429,
        "detail": "Too many requests from this address; retry in a minute"}
UNAVAILABLE = {"type": "urn:qupai:problem:unavailable", "title": "Unavailable", "status": 503,
               "detail": "The service is unreachable; retry shortly"}
PROBLEM = "application/problem+json"
WRITE = "/v1/instruments/signal_bench_sim_1_dg822_pro_usb/write/set_output"
READ = "/v1/instruments/signal_bench_sim_1_tbs1104_usb/read/acquire"


class Clock:
    """A clock that a wait moves on: the Lab's ``_clock`` and ``_sleep``."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


@pytest.fixture
def clock(lab: Lab) -> Clock:
    c = Clock()
    lab._clock, lab._sleep = c, c.sleep
    return c


def rate_limited(fake: FakeInstruCore, method: str, path: str, times: int = 1, retry_after: str | None = "60",
                 body: Any = None, status: int = 429, content_type: str = PROBLEM) -> None:
    """The gateway refuses ``times`` calls to ``method path`` before they reach the lab."""
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    fake.respond(method, path, status, RATE if body is None else body, headers=headers, content_type=content_type,
                 times=times)


# ── the platform's 429, for a call that may be sent again ───────────────────────────────────────


def test_a_read_waits_for_retry_after_and_is_sent_again(lab: Lab, fake: FakeInstruCore, clock: Clock, capsys) -> None:
    rate_limited(fake, "GET", "/v1/machines", times=2)
    assert len(lab.machines()) == 3
    assert clock.slept == [60.0, 60.0]
    assert len(fake.calls("GET", "/v1/machines")) == 3
    err = capsys.readouterr().err
    assert err.count("rate-limited by the platform") == 1  # said once, not at every wait
    assert ("rate-limited by the platform; waiting 1 min, then asking again (GET /v1/machines: Too many requests "
            "from this address; retry in a minute). One call waits 5 min at most") in err
    lab.close()
    line = lines(lab.recorder.path)[-1]
    assert (line["ok"], line["status"], line["retries"], line["waited_s"]) == (True, 200, 2, 120.0)


def test_retry_after_is_bounded_each_time_and_in_all(lab: Lab, fake: FakeInstruCore, clock: Clock, capsys) -> None:
    rate_limited(fake, "GET", "/v1/machines", times=20, retry_after="3600")
    with pytest.raises(LabError) as e:
        lab.machines()
    assert clock.slept == [60.0] * 5  # 60 s at most each time, 5 minutes in all
    assert len(fake.calls("GET", "/v1/machines")) == 6
    assert (e.value.code, e.value.status) == ("RATE", 429)
    assert (e.value.detail["retries"], e.value.detail["waited_s"]) == (5, 300.0)
    err = capsys.readouterr().err
    assert "the platform still refuses GET /v1/machines after 5 min of waiting (429 RATE); giving up" in err
    line = lines(lab.recorder.path)[-1]
    assert (line["ok"], line["code"], line["retries"], line["waited_s"]) == (False, "RATE", 5, 300.0)


def test_without_retry_after_the_wait_doubles(lab: Lab, fake: FakeInstruCore, clock: Clock) -> None:
    rate_limited(fake, "GET", "/v1/machines", times=20, retry_after=None)
    with pytest.raises(LabError):
        lab.machines()
    assert clock.slept == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]  # 242 s; one more would pass 5 minutes


@pytest.mark.parametrize(("header", "wait"), [("0", 1.0), ("2.5", 2.5), ("soon", 2.0)])
def test_retry_after_of_zero_or_unreadable(lab: Lab, fake: FakeInstruCore, clock: Clock, header: str,
                                           wait: float) -> None:
    rate_limited(fake, "GET", "/v1/machines", retry_after=header)
    lab.machines()
    assert clock.slept == [wait]  # never sooner than 1 s; an unreadable one is as none


def test_retry_after_as_an_http_date() -> None:
    assert pace.retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0.0
    assert 3500 < (pace.retry_after("Fri, 31 Dec 2100 23:59:59 GMT") or 0)
    assert pace.retry_after(None) is None and pace.retry_after(" ") is None


def test_session_start_and_end_are_sent_again(lab: Lab, fake: FakeInstruCore, clock: Clock) -> None:
    """The fault of 2026-11-26: the block's end (sessions/end) failed too. A session's start, renewal and end are
    sent again after any 429, the gateway's or not."""
    rate_limited(fake, "POST", "/v1/machines/signal-bench-sim/sessions")
    plain = {"error": "too many requests"}
    with lab.machine("signal-bench-sim") as m:
        rate_limited(fake, "POST", "/v1/machines/signal_bench_sim_1/sessions/renew", body=plain,
                     content_type="application/json")
        m.renew()
        rate_limited(fake, "POST", "/v1/machines/signal_bench_sim_1/sessions/end", times=2)
    assert fake.sessions == {}  # ended after all
    assert len(fake.calls("POST", "/v1/machines/signal_bench_sim_1/sessions/end")) == 3
    assert clock.slept == [60.0, 60.0, 60.0, 60.0]


def test_read_and_check_ops_are_sent_again(lab: Lab, fake: FakeInstruCore, clock: Clock) -> None:
    with lab.machine("signal-bench-sim") as m:
        rate_limited(fake, "POST", READ, body={"error": "slow down"}, content_type="application/json")
        assert m.role("tbs1104_usb").read("acquire", channel=1)["channel"] == 1
        rate_limited(fake, "POST", "/v1/instruments/signal_bench_sim_1_dg822_pro_usb/check/set_output")
        assert m.role("dg822_pro_usb").check("set_output", channel=1, on=True)["ok"] is True
    assert clock.slept == [60.0, 60.0]


def test_the_manifest_counts_the_waits(fake: FakeInstruCore, tmp_path: Path, clock: Clock) -> None:
    folder = _runs.RunFolder(tmp_path / "exp", "20261126-000000-abcd", live=False)
    lab = Lab(fake.url, "qpk_test", script_ref="", run_folder=folder)
    lab._clock, lab._sleep, lab.poll_s = clock, clock.sleep, 0
    rate_limited(fake, "GET", "/v1/machines", times=2, retry_after="30")
    lab.machines()
    kept = json.loads((folder.path / "manifest.json").read_text())["rate_limited"]
    assert (kept["waits"], kept["waited_s"]) == (2, 60.0) and kept["first_at"] and kept["last_at"]


# ── a write ─────────────────────────────────────────────────────────────────────────────────────


def test_a_write_the_gateway_refused_at_its_door_is_sent_again(lab: Lab, fake: FakeInstruCore, clock: Clock) -> None:
    """The gateway answers 429 before it passes a call on (qupai-gateway internal/restproxy: the address's calls per
    minute, the member's calls to the lab): the write never reached the lab, so it is sent again."""
    with lab.machine("signal-bench-sim") as m:
        rate_limited(fake, "POST", WRITE)
        m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert len(fake.calls("POST", WRITE)) == 2 and clock.slept == [60.0]
    assert len(lab._writes) == 1  # one change, not two


@pytest.mark.parametrize(("status", "body", "content_type"), [
    (429, {"error": {"code": "TOO_MANY", "message": "slow down", "detail": {}}}, "application/json"),  # not the gateway
    (429, b"Too Many Requests", "text/plain"),
    (503, UNAVAILABLE, PROBLEM),  # may come after the call reached the lab (the gateway's upstream failed)
])
def test_a_write_that_may_have_reached_the_lab_is_never_sent_twice(
    lab: Lab, fake: FakeInstruCore, clock: Clock, capsys, status: int, body: Any, content_type: str
) -> None:
    with lab.machine("signal-bench-sim") as m:
        rate_limited(fake, "POST", WRITE, status=status, body=body, content_type=content_type)
        with pytest.raises(LabError) as e:
            m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    assert e.value.status == status
    assert len(fake.calls("POST", WRITE)) == 1 and clock.slept == []
    assert "is not sent again: a write that may have reached the lab is never sent twice" in capsys.readouterr().err


def test_a_503_is_waited_for_only_when_it_names_retry_after(lab: Lab, fake: FakeInstruCore, clock: Clock) -> None:
    rate_limited(fake, "GET", "/v1/machines", status=503, body=UNAVAILABLE, retry_after="5")
    lab.machines()
    assert clock.slept == [5.0]
    rate_limited(fake, "GET", "/v1/machines", status=503, body=UNAVAILABLE, retry_after=None)
    with pytest.raises(LabError) as e:
        lab.machines()
    assert e.value.code == "UNAVAILABLE" and clock.slept == [5.0]


def test_a_signal_while_a_session_start_waits_stops_the_script(lab: Lab, fake: FakeInstruCore) -> None:
    """A session's start is sent while signals wait (stop.shielded). A signal that comes while it waits for the
    platform stops the script there: nothing was started, so nothing is left held."""
    rate_limited(fake, "POST", "/v1/machines/signal-bench-sim/sessions", times=10)
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        stop._handler(signal.SIGTERM, None)  # SIGTERM comes while it waits

    lab._sleep = sleep
    with pytest.raises(SystemExit) as e:
        with lab.machine("signal-bench-sim"):
            pytest.fail("the block must not run")
    assert e.value.code == 128 + signal.SIGTERM
    assert slept == [60.0]  # it stopped in its first wait, and asked no more
    assert len(fake.calls("POST", "/v1/machines/signal-bench-sim/sessions")) == 1
    assert fake.sessions == {} and fake.requests == {} and stop.stopping() is None


# ── the first question of Lab.from_qupai ────────────────────────────────────────────────────────


def test_the_lab_access_question_waits_too(fake: FakeInstruCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"server": fake.url, "api_key": "qpk_person"}))
    monkeypatch.setenv(direct.CREDENTIALS_ENV, str(creds))
    slept: list[float] = []
    monkeypatch.setattr(direct, "_sleep", slept.append)
    fake.respond("GET", direct.LAB_ACCESS, 429, RATE, headers={"Retry-After": "60"}, content_type=PROBLEM)
    fake.respond("GET", direct.LAB_ACCESS, 200, {"labs": [{"lab": "sim", "listing": "lab-sim", "role": "operate"}]})
    lab = Lab.from_qupai("sim", runs=False)
    assert lab.url.endswith("/rest/lab-sim") and slept == [60.0]
    assert f"rate-limited by the platform; waiting 1 min, then asking again (GET {direct.LAB_ACCESS}" in (
        capsys.readouterr().err)


# ── a block that waits for its machine says so ──────────────────────────────────────────────────


def _wang_holds_the_bench(fake: FakeInstruCore) -> None:
    fake.sessions["bench"] = {"session_id": "s_wang", "machine": "bench", "client_id": "qupai:t:wang",
                              "client_name": "Wang", "purpose": "angle scan", "started_at": "2026-11-26T05:25:19.000Z",
                              "expires_in_s": 900.0, "max_expires_in_s": 1500.0}
    fake.holders["bench"] = "qpk_wang"


def test_waiting_for_a_held_bench_says_who_holds_it_and_every_30_s(fake: FakeInstruCore, tmp_path: Path,
                                                                   capsys) -> None:
    fake.direct_keys.add("qpk_test")
    _wang_holds_the_bench(fake)
    fake.polls_until_offer, fake.start_position = 10, 1
    folder = _runs.RunFolder(tmp_path / "exp", "20261126-000000-abcd", live=False)
    lab = Lab(fake.url, "qpk_test", script_ref="", run_folder=folder, direct=True)
    clock = Clock()
    lab._clock, lab._sleep, lab.poll_s = clock, clock.sleep, 2.0
    with lab.machine("signal-bench") as m:
        assert m.held
        during = json.loads((folder.path / "manifest.json").read_text())["waits"]
    # looked at every 2 s at first, then less often up to 15 s: never in a tight loop
    assert clock.slept == [2.0, 4.0, 8.0, 15.0, 15.0, 15.0, 15.0, 15.0, 15.0, 15.0]
    err = [x.removeprefix("qupai-lab: ") for x in capsys.readouterr().err.splitlines()]
    assert err[0] == ("queued for signal-bench at position 1; waiting for its turn: held by Wang since 2026-11-26 "
                      "05:25:19 UTC; its session ends in 25 min at the latest. It says how the wait goes every 30 s; "
                      "SIGINT or SIGTERM leaves the queue")
    still = [x for x in err if x.startswith("still waiting")]
    assert still == [
        "still waiting for signal-bench (44 s): position 1; held by Wang since 2026-11-26 05:25:19 UTC; its session "
        "ends in 25 min at the latest",
        "still waiting for signal-bench (1 min 14 s): position 1; held by Wang since 2026-11-26 05:25:19 UTC; its "
        "session ends in 25 min at the latest",
        "still waiting for signal-bench (1 min 44 s): position 1; held by Wang since 2026-11-26 05:25:19 UTC; its "
        "session ends in 25 min at the latest",
    ]
    assert "signal-bench is held after waiting 1 min 59 s in its queue" in err
    # the holder is looked up again for each of those lines, not at every look at the queue
    assert len(fake.calls("GET", "/v1/machines/signal-bench")) == 3
    wait = json.loads((folder.path / "manifest.json").read_text())["waits"]
    assert len(wait) == 1 and during[0]["outcome"] == "held"  # kept as the wait ended, before the block's body
    assert {k: wait[0][k] for k in ("machine", "for", "position", "waited_s", "outcome")} == {
        "machine": "signal-bench", "for": "its turn", "position": 1, "waited_s": 119.0, "outcome": "held"}
    assert wait[0]["holder"] == {"name": "Wang", "since": "2026-11-26T05:25:19.000Z", "run": None, "ends_in_s": 1500.0}
    assert wait[0]["since"] and wait[0]["until"] and wait[0]["lab_said"]


def test_the_manifest_says_the_wait_while_it_lasts_and_how_it_ended(fake: FakeInstruCore, tmp_path: Path) -> None:
    fake.polls_until_offer = 100
    folder = _runs.RunFolder(tmp_path / "exp", "20261126-000000-abcd", live=False)
    lab = Lab(fake.url, "qpk_test", script_ref="", run_folder=folder)
    clock = Clock()
    seen: list[dict[str, Any]] = []

    def sleep(s: float) -> None:
        clock.sleep(s)
        seen.append(json.loads((folder.path / "manifest.json").read_text())["waits"][0])

    lab._clock, lab._sleep, lab.poll_s = clock, sleep, 2.0
    with pytest.raises(LabError) as e:
        with lab.machine("signal-bench-sim", wait_s=60):
            pytest.fail("the block must not run")
    assert e.value.code == "WAIT_TIMEOUT"
    assert seen[0]["outcome"] == "waiting" and seen[0]["until"] is None
    done = json.loads((folder.path / "manifest.json").read_text())["waits"][0]
    assert done["outcome"] == "timeout" and done["until"] and done["waited_s"] >= 60


def test_waiting_for_a_copy_says_how_many_are_in_use(lab: Lab, fake: FakeInstruCore, capsys) -> None:
    fake._session("signal_bench_sim_1", "someone")
    fake._session("signal_bench_sim_2", "someone else")
    fake.polls_until_offer = 2
    with lab.machine("signal-bench-sim") as m:
        assert m.held
    err = capsys.readouterr().err
    assert ("queued for signal-bench-sim at position 2; waiting for a copy: all 2 copies are in use. It says how "
            "the wait goes every 30 s") in err


def test_a_member_at_their_cap_is_told_so(lab: Lab, fake: FakeInstruCore, capsys) -> None:
    fake.polls_until_offer = 1
    queued = {"error": {"code": "SESSION_PENDING", "message": "you hold 2 simulated sessions",
                        "detail": {"request_id": "q_cap", "position": 1, "reason": "member_cap"}},
              "request": {"request_id": "q_cap", "queue": "signal_bench_sim", "status": "pending", "position": 1,
                          "purpose": "", "wait_reason": "member_cap"}}
    fake.requests["q_cap"] = dict(queued["request"])
    fake.respond("POST", "/v1/machines/signal-bench-sim/sessions", 202, queued)
    with lab.machine("signal-bench-sim"):
        pass
    assert "(you hold as many simulated sessions as a member may); waiting for a copy" in capsys.readouterr().err


# ── the official moku package's calls, at the local stand-in ────────────────────────────────────


def _moku(address: str, path: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(f"http://{address}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


DESCRIBE = "/v1/moku/signal-bench-sim-1/moku_go/api/moku/describe"


def test_the_stand_in_waits_for_a_short_retry_after(lab: Lab, fake: FakeInstruCore, capsys) -> None:
    with lab.machine("signal-bench-sim") as m:
        address = m.moku("moku_go")["ip"]
        slept: list[float] = []
        m._forwarders["moku_go"]._sleep = slept.append
        rate_limited(fake, "GET", DESCRIBE, retry_after="2")
        status, answer = _moku(address, "/api/moku/describe")
    assert (status, answer["success"]) == (200, True) and slept == [2.0]
    assert len(fake.calls("GET", DESCRIBE)) == 2
    assert "the moku package's call waits 2 s here, then is sent again" in capsys.readouterr().err


def test_the_stand_in_refuses_in_the_mokus_envelope_past_the_packages_patience(lab: Lab, fake: FakeInstruCore,
                                                                               capsys) -> None:
    with lab.machine("signal-bench-sim") as m:
        address = m.moku("moku_go")["ip"]
        slept: list[float] = []
        m._forwarders["moku_go"]._sleep = slept.append
        rate_limited(fake, "GET", DESCRIBE, times=2)
        first = _moku(address, "/api/moku/describe")
        second = _moku(address, "/api/moku/describe")
        third = _moku(address, "/api/moku/describe")
    assert slept == [] and len(fake.calls("GET", DESCRIBE)) == 3
    status, answer = first
    assert (status, answer["success"], answer["code"]) == (200, False, "RATE")
    assert answer["messages"] == ["rate-limited by the platform (Too many requests from this address; retry in a "
                                  "minute): this call of the moku package did not reach the lab; the platform asks to "
                                  "wait 1 min"]
    assert second[1]["code"] == "RATE" and third[1]["success"] is True
    assert capsys.readouterr().err.count("rate-limited by the platform") == 1


def test_the_stand_in_passes_back_another_429_as_it_came(lab: Lab, fake: FakeInstruCore) -> None:
    with lab.machine("signal-bench-sim") as m:
        address = m.moku("moku_go")["ip"]
        rate_limited(fake, "GET", DESCRIBE, body={"error": {"code": "TOO_MANY", "message": "x", "detail": {}}},
                     content_type="application/json")
        status, answer = _moku(address, "/api/moku/describe")
    assert status == 429 and answer["error"]["code"] == "TOO_MANY"
