"""``qupai-lab run`` end to end against the fake InstruCore: the key file, the checkout, the script's environment,
the outputs, the exit code and the signals."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from conftest import lines
from fake_instrucore import FakeInstruCore
from test_live_run import points

from qupai_lab import runner
from qupai_lab.cli import main

EXPERIMENT = "experiments/2026-11-21-freq"
RUN_KEY = "qrk_run"

RECORDING = """\
import json, os, sys
from pathlib import Path
from qupai_lab import Lab

lab = Lab.from_env(record="calls.jsonl")
Path("data").mkdir(exist_ok=True)
Path("data/env.json").write_text(json.dumps({
    "cwd": os.getcwd(), "argv": sys.argv,
    "env": {k: v for k, v in os.environ.items() if k.startswith("QUPAI_") or k == "PYTHONUNBUFFERED"},
}))
with lab.machine(sys.argv[1]) as m:
    m.role("dg822_pro_usb").write("set_waveform", channel=1, frequency_hz=1000)
lab.changes("changes.json")
Path("figures").mkdir(exist_ok=True)
Path("figures/response.txt").write_text("drawn")
sys.exit(3)
"""

FAILING = """\
import sys
from qupai_lab import Lab

lab = Lab.from_env(record="calls.jsonl")
with lab.machine(sys.argv[1]) as m:
    m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    raise RuntimeError("the script failed")
"""

WAITING = """\
import sys, time
from qupai_lab import Lab

lab = Lab.from_env(record="calls.jsonl")
with lab.machine(sys.argv[1]) as m:
    m.role("dg822_pro_usb").write("set_output", channel=1, on=True)
    time.sleep(60)
"""

DEAF = """\
import signal, time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path("data").mkdir()
Path("data/started").write_text("1")
time.sleep(60)
"""


def git(place: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(place), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def make_place(tmp_path: Path, script: str) -> tuple[Path, str]:
    """A lab place's repository with one experiment and a rehearsal's outputs committed beside its run.py."""
    place = tmp_path / "place"
    folder = place / EXPERIMENT
    (folder / "data").mkdir(parents=True)
    (place / ".gitignore").write_text(".env\n.qupai/\n")
    (folder / "run.py").write_text(script)
    (folder / "calls.jsonl").write_text('{"rehearsal": true}\n')
    (folder / "changes.json").write_text('{"machines": ["signal-bench-sim-1"]}\n')
    (folder / "data" / "response.csv").write_text("rehearsal\n")
    git(place, "init", "-q", "-b", "main")
    git(place, "add", "-A")
    git(place, "commit", "-qm", "the experiment")
    return place, git(place, "rev-parse", "HEAD")


def key_file(place: Path, run: str, url: str, machine: str = "signal-bench", **drop: bool) -> str:
    values = {"QUPAI_LAB_URL": url, "QUPAI_LAB_KEY": RUN_KEY, "QUPAI_LAB_RUN": run, "QUPAI_LAB_RUN_MACHINE": machine}
    rel = f".qupai/runs/{run}/key.env"
    path = place / rel
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if not drop.get(k)))
    return rel


@pytest.fixture
def run_id() -> str:
    run = f"job_{uuid.uuid4().hex[:12]}"
    yield run
    assert not (runner.run_root() / f"qupai-run-{run}").exists()


@pytest.fixture
def live(fake: FakeInstruCore) -> FakeInstruCore:
    fake.run_keys.add(RUN_KEY)
    return fake


def argv(rel: str, sha: str, machine: str = "signal-bench") -> list[str]:
    return ["run", "--key-file", rel, "--commit", sha, EXPERIMENT, machine]


def test_run_end_to_end(live: FakeInstruCore, tmp_path: Path, run_id: str, monkeypatch: pytest.MonkeyPatch,
                        capfd) -> None:
    place, sha = make_place(tmp_path, RECORDING)
    rel = key_file(place, run_id, live.url)
    monkeypatch.setenv("QUPAI_LAB_KEY", "qpk_project")  # the project's own: not the run's
    monkeypatch.setenv("QUPAI_SECRET_OF_THE_SANDBOX", "x")
    monkeypatch.chdir(place)
    assert main(argv(rel, sha)) == 3  # the script's exit code

    assert not (place / rel).exists() and not (place / rel).parent.exists()  # the key file is gone
    tree = runner.run_root() / f"qupai-run-{run_id}"
    assert not tree.exists()
    assert git(place, "worktree", "list", "--porcelain").count("worktree ") == 1

    runs = place / EXPERIMENT / "runs" / run_id
    assert sorted(p.relative_to(runs).as_posix() for p in runs.rglob("*") if p.is_file()) == [
        "calls.jsonl", "changes.json", "data/env.json", "figures/response.txt"]  # no rehearsal output
    seen = json.loads((runs / "data/env.json").read_text())
    assert Path(seen["cwd"]).resolve() == (tree / EXPERIMENT).resolve()
    assert seen["argv"] == ["run.py", "signal-bench"]
    assert seen["env"] == {"QUPAI_LAB_URL": live.url, "QUPAI_LAB_KEY": RUN_KEY, "QUPAI_LAB_RUN": run_id,
                           "QUPAI_LAB_RUN_MACHINE": "signal-bench", "PYTHONUNBUFFERED": "1"}
    short = git(place, "rev-parse", "--short", "HEAD")
    changes = json.loads((runs / "changes.json").read_text())
    assert changes["script"] == f"{EXPERIMENT}/run.py@{short}" and changes["machines"] == ["signal-bench"]
    recorded = [(x["method"], x["path"]) for x in lines(runs / "calls.jsonl")]
    assert recorded == [("GET", "/v1/machines"), ("POST", "/v1/machines/signal-bench/sessions"),
                        ("POST", "/v1/instruments/dg822_pro_usb/write/set_waveform"),
                        ("POST", "/v1/machines/bench/sessions/end")]

    start = live.calls("POST", "/v1/machines/signal-bench/sessions")
    assert [s.body for s in start] == [{"purpose": f"run {run_id}", "queue": True}]
    assert start[0].headers["Authorization"] == f"Bearer {RUN_KEY}"
    assert start[0].headers["X-InstruCore-Script"] == f"{EXPERIMENT}/run.py@{short}"
    assert live.sessions == {}

    out, err = capfd.readouterr()
    assert points(out) == [("held", 1), ("calls", 1), ("held", 0)]
    assert f"run {run_id}: {EXPERIMENT}/run.py at {sha[:7]} on signal-bench" in err
    assert (f"run {run_id}: the script exited with 3; kept data/, figures/, calls.jsonl, changes.json in "
            f"{EXPERIMENT}/runs/{run_id}/") in err


def test_a_failing_script_still_leaves_its_outputs(live: FakeInstruCore, tmp_path: Path, run_id: str,
                                                   monkeypatch: pytest.MonkeyPatch, capfd) -> None:
    place, sha = make_place(tmp_path, FAILING)
    rel = key_file(place, run_id, live.url)
    monkeypatch.chdir(place)
    assert main(argv(rel, sha)) == 1
    runs = place / EXPERIMENT / "runs" / run_id
    assert sorted(p.name for p in runs.iterdir()) == ["calls.jsonl"]
    end = live.calls("POST", "/v1/machines/bench/sessions/end")
    assert [s.body["reason"] for s in end] == ["qupai-lab: the script's block raised"]
    assert "RuntimeError: the script failed" in capfd.readouterr().err


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"drop": "QUPAI_LAB_KEY"}, "has no QUPAI_LAB_KEY"),
        ({"machine": "other-bench"}, "is on other-bench, not signal-bench"),
        ({"commit": "0" * 40}, "has no commit"),
        ({"commit": "--orphan"}, "is not a commit"),
        ({"experiment": "../elsewhere"}, "is not a folder inside the repository"),
        ({"experiment": "experiments/none"}, "experiments/none/run.py is not in commit"),
    ],
)
def test_a_run_that_cannot_start(live: FakeInstruCore, tmp_path: Path, run_id: str, monkeypatch: pytest.MonkeyPatch,
                                 capfd, change: dict[str, str], says: str) -> None:
    place, sha = make_place(tmp_path, RECORDING)
    drop = {change["drop"]: True} if "drop" in change else {}
    rel = key_file(place, run_id, live.url, change.get("machine", "signal-bench"), **drop)
    monkeypatch.chdir(place)
    args = ["run", "--key-file", rel, f"--commit={change.get('commit', sha)}", change.get("experiment", EXPERIMENT),
            "signal-bench"]
    assert main(args) == runner.CANNOT_START
    assert not (place / rel).exists()  # deleted also when the run cannot start
    assert says in capfd.readouterr().err
    assert live.calls("POST") == [] and not (place / EXPERIMENT / "runs").exists()
    assert git(place, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_outside_a_repository(live: FakeInstruCore, tmp_path: Path, run_id: str, monkeypatch: pytest.MonkeyPatch,
                              capfd) -> None:
    rel = key_file(tmp_path, run_id, live.url)
    monkeypatch.chdir(tmp_path)
    assert main(argv(rel, "abc1234")) == runner.CANNOT_START
    assert not (tmp_path / rel).exists() and "is not in a git repository" in capfd.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_ends_the_runs_session_and_keeps_what_it_wrote(live: FakeInstruCore, tmp_path: Path,
                                                               run_id: str) -> None:
    place, sha = make_place(tmp_path, WAITING)
    rel = key_file(place, run_id, live.url)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUPAI_")}
    proc = subprocess.Popen([sys.executable, "-m", "qupai_lab", *argv(rel, sha)], cwd=place, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [seen.put(x) for x in proc.stdout], daemon=True).start()
    while points(seen.get(timeout=30)) != [("calls", 1)]:  # held, and the first write is made
        pass
    proc.send_signal(signal.SIGTERM)  # to the runner only: it passes the signal on
    assert proc.wait(timeout=30) == 143
    err = proc.stderr.read()
    assert "SIGTERM: stopping the script (killed after 20 s)" in err
    assert f"the script exited with 143; kept calls.jsonl in {EXPERIMENT}/runs/{run_id}/" in err
    end = live.calls("POST", "/v1/machines/bench/sessions/end")
    assert [s.body["reason"] for s in end] == ["qupai-lab: stopped by SIGTERM"]
    assert live.sessions == {}
    recorded = lines(place / EXPERIMENT / "runs" / run_id / "calls.jsonl")
    assert recorded[-1]["path"] == "/v1/machines/bench/sessions/end" and recorded[-1]["ok"] is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_a_script_that_does_not_stop_is_killed_after_the_grace(fake: FakeInstruCore, tmp_path: Path, run_id: str,
                                                                monkeypatch: pytest.MonkeyPatch, capfd) -> None:
    monkeypatch.setattr(runner, "GRACE_S", 0.5)
    place, sha = make_place(tmp_path, DEAF)
    rel = key_file(place, run_id, fake.url)
    monkeypatch.chdir(place)
    started = runner.run_root() / f"qupai-run-{run_id}" / EXPERIMENT / "data" / "started"

    def stop_it() -> None:
        deadline = time.monotonic() + 30
        while not started.exists():
            if time.monotonic() > deadline:
                return
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)  # this process runs the runner: its handler passes it on

    before = signal.getsignal(signal.SIGTERM)
    threading.Thread(target=stop_it, daemon=True).start()
    assert main(argv(rel, sha)) == 128 + signal.SIGKILL
    assert signal.getsignal(signal.SIGTERM) == before
    assert (place / EXPERIMENT / "runs" / run_id / "data" / "started").read_text() == "1"
    assert "did not end within 0.5 s: killed" in capfd.readouterr().err
