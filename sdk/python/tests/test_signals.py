"""SIGTERM and SIGINT in a script that holds a machine end its session before the script exits."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import lines
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, stop

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")

HOLDING = """\
import sys, time
from qupai_lab import Lab

lab = Lab(sys.argv[1], sys.argv[2], script_ref="")
lab.poll_s = 0.05
with lab.machine(sys.argv[3]) as m:
    m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    print("ready", flush=True)
    time.sleep(60)
print("not reached", flush=True)
"""


def start(tmp_path: Path, fake: FakeInstruCore, machine: str) -> subprocess.Popen[str]:
    script = tmp_path / "hold.py"
    script.write_text(HOLDING)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUPAI_")}
    return subprocess.Popen([sys.executable, str(script), fake.url, "qpk_test", machine], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def first_line(proc: subprocess.Popen[str], timeout: float = 20.0) -> str:
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
    return lines.get(timeout=timeout).strip()


def until(check, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_sigterm_ends_the_held_copy_before_the_script_exits(fake: FakeInstruCore, tmp_path: Path) -> None:
    proc = start(tmp_path, fake, "signal-bench-sim")
    assert first_line(proc) == "ready"
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 143
    end = fake.calls("POST", "/v1/machines/signal_bench_sim_1/sessions/end")
    assert [s.body["reason"] for s in end] == ["qupai-lab: stopped by SIGTERM"]
    assert fake.sessions == {}
    assert "not reached" not in out and "SIGTERM: releasing the machine, then stopping" in err


def test_sigint_ends_the_held_copy_too(fake: FakeInstruCore, tmp_path: Path) -> None:
    proc = start(tmp_path, fake, "signal-bench-sim")
    assert first_line(proc) == "ready"
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=20)
    assert proc.returncode in (-signal.SIGINT, 130)  # an uncaught KeyboardInterrupt
    assert "KeyboardInterrupt" in err
    end = fake.calls("POST", "/v1/machines/signal_bench_sim_1/sessions/end")
    assert [s.body["reason"] for s in end] == ["qupai-lab: stopped by SIGINT"]
    assert fake.sessions == {}


def test_sigterm_while_queued_leaves_the_queue(fake: FakeInstruCore, tmp_path: Path) -> None:
    fake.polls_until_offer = 10**6
    proc = start(tmp_path, fake, "signal-bench-sim")
    until(lambda: fake.calls("GET", "/v1/session-requests/q_0001"))
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=20)
    assert proc.returncode == 143
    assert [s.path for s in fake.calls("DELETE")] == ["/v1/session-requests/q_0001"]
    assert fake.requests["q_0001"]["status"] == "cancelled" and fake.sessions == {}


def test_handlers_are_installed_for_the_block_and_restored(lab: Lab) -> None:
    before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    with lab.machine("signal-bench-sim"):
        assert signal.getsignal(signal.SIGTERM) is stop._handler
        assert signal.getsignal(signal.SIGINT) is stop._handler
        with lab.machine("signal-bench-sim-2"):  # nested: one handler, restored when the last is released
            pass
        assert signal.getsignal(signal.SIGTERM) is stop._handler
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before
    with lab.machine("signal-bench"):  # a live machine that is not held installs nothing
        assert signal.getsignal(signal.SIGTERM) == before[0]


def test_an_ignored_signal_stays_ignored(lab: Lab) -> None:
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with lab.machine("signal-bench-sim"):
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_a_signal_while_the_session_ends_waits_for_the_end(lab: Lab, fake: FakeInstruCore) -> None:
    fake.before[("POST", "/v1/machines/signal_bench_sim_1/sessions/end")] = lambda: (
        os.kill(os.getpid(), signal.SIGTERM), time.sleep(0.2))
    with pytest.raises(SystemExit) as e:
        with lab.machine("signal-bench-sim"):
            pass
    assert e.value.code == 143
    assert len(fake.calls("POST", "/v1/machines/signal_bench_sim_1/sessions/end")) == 1
    assert fake.sessions == {} and stop.stopping() is None
    ended = [x for x in lines(lab.recorder.path) if x["path"].endswith("/sessions/end")]
    assert [(x["ok"], x["status"]) for x in ended] == [(True, 200)]  # the answer was read, not cut off


def test_a_repeated_signal_while_stopping_is_ignored(lab: Lab, fake: FakeInstruCore) -> None:
    # A runner passes on the SIGTERM its process group also got: the second must not cut the release short.
    fake.before[("POST", "/v1/machines/signal_bench_sim_1/sessions/end")] = lambda: (
        os.kill(os.getpid(), signal.SIGTERM), time.sleep(0.2))
    with pytest.raises(SystemExit) as e:
        with lab.machine("signal-bench-sim"):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            pytest.fail("SIGTERM must stop the block")
    assert e.value.code == 143
    end = fake.calls("POST", "/v1/machines/signal_bench_sim_1/sessions/end")
    assert [s.body["reason"] for s in end] == ["qupai-lab: stopped by SIGTERM"]
    assert fake.sessions == {}
    ended = [x for x in lines(lab.recorder.path) if x["path"].endswith("/sessions/end")]
    assert [(x["ok"], x["status"]) for x in ended] == [(True, 200)]
