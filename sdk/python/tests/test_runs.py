"""A rehearsal's run folder: where ``Lab.from_env()`` puts a script's outputs, the manifest, the inputs, the card's
``changes.json`` and the experiments' index."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import lines
from fake_instrucore import FakeInstruCore

from qupai_lab import runs

EXPERIMENT = "experiments/2026-11-22-freq"

REHEARSAL = """\
import sys
from qupai_lab import Lab

lab = Lab.from_env(record="calls.jsonl")
sweep = lab.input("sweep.json")
with lab.machine(sys.argv[1]) as m:
    gen, scope = m.role("dg822_pro_usb"), m.role("tbs1104_usb")
    rows = []
    for f in sweep["frequencies_hz"]:
        gen.write("set_waveform", channel=1, shape="sine", frequency_hz=f, amplitude_v=1.0)
        trace = scope.read("acquire", channel=1, points=10)
        rows.append({"frequency_hz": f, **lab.save(f"data/raw/trace_{f:g}.json", trace)})
lab.table("data/response.csv", rows)
lab.changes("changes.json")
print(lab.out)
if len(sys.argv) > 2:
    raise RuntimeError(sys.argv[2])
"""


def git(place: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(place), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def project(tmp_path: Path, url: str, script: str = REHEARSAL) -> tuple[Path, Path, str]:
    """A lab project: its .env, an experiment with run.py and inputs/sweep.json, committed."""
    place = tmp_path / "project"
    folder = place / EXPERIMENT
    (folder / "inputs").mkdir(parents=True)
    (place / ".gitignore").write_text(".env\n.qupai/\n**/data/raw/\n")
    (place / ".env").write_text(f"QUPAI_LAB_URL={url}\nQUPAI_LAB_KEY=qpk_test\n")
    (folder / "README.md").write_text("# Frequency response | signal bench\n\nWhat was asked.\n")
    (folder / "run.py").write_text(script)
    (folder / "inputs" / "sweep.json").write_text('{"frequencies_hz": [100, 1000]}\n')
    git(place, "init", "-q", "-b", "main")
    git(place, "add", "-A")
    git(place, "commit", "-qm", "the experiment")
    return place, folder, git(place, "rev-parse", "HEAD")


def run(folder: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("QUPAI_")} | env
    return subprocess.run([sys.executable, "run.py", *args], cwd=folder, env=clean, capture_output=True, text=True,
                          timeout=60, check=False)


def test_a_rehearsal_has_its_run_folder(fake: FakeInstruCore, tmp_path: Path) -> None:
    place, folder, sha = project(tmp_path, fake.url)
    done = run(folder, "signal-bench-sim")
    assert done.returncode == 0, done.stderr
    out = Path(done.stdout.strip())
    [only] = (folder / "runs").iterdir()
    assert out == only and len(only.name) == len("20261122-021116-a3f9")

    # The experiment's folder keeps only what a person wrote, and the card's changes.json.
    assert sorted(p.name for p in folder.iterdir()) == ["README.md", "changes.json", "inputs", "run.py", "runs"]
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "calls.jsonl", "changes.json", "data/raw/trace_100.json", "data/raw/trace_1000.json", "data/response.csv",
        "manifest.json"]
    card, own = json.loads((folder / "changes.json").read_text()), json.loads((out / "changes.json").read_text())
    assert card == own and card["run"] == out.name and card["script_blob"] == runs.blob_id((folder / "run.py")
                                                                                           .read_bytes())
    assert [x["file"] for x in lines(out / "calls.jsonl") if "file" in x] == ["data/raw/trace_100.json",
                                                                              "data/raw/trace_1000.json"]

    m = json.loads((out / "manifest.json").read_text())
    assert {k: m[k] for k in ("run", "kind", "simulated", "experiment", "script", "argv", "commit", "dirty", "lab",
                              "status", "exit_code", "calls", "raw_files")} == {
        "run": out.name, "kind": "rehearsal", "simulated": True, "experiment": EXPERIMENT, "script": "run.py",
        "argv": ["signal-bench-sim"], "commit": sha, "dirty": False, "lab": None, "status": "ended",
        "exit_code": None, "calls": 4, "raw_files": 2}
    assert m["script_blob"] == card["script_blob"]
    assert m["inputs"] == [{"path": "inputs/sweep.json", "bytes": 32,
                            "blob": runs.blob_id(b'{"frequencies_hz": [100, 1000]}\n')}]
    [held] = m["machines"]
    assert (held["requested"], held["kind"], held["held"]) == ("signal-bench-sim", "simulated", True)
    assert held["machine"].startswith("signal-bench-sim-") and held["session"].startswith("s_")
    assert held["held_at"] <= held["released_at"]
    assert m["started_at"] <= m["ended_at"]
    assert m["outputs"] == ["calls.jsonl", "changes.json", "data/response.csv"]
    assert m["versions"]["qupai_lab"] == runs.__version__

    index = (place / "experiments" / "INDEX.md").read_text()
    assert "| 2026-11-22 | [Frequency response \\| signal bench](2026-11-22-freq/) | 1 rehearsal |" in index
    assert f"| [{out.name}](2026-11-22-freq/runs/{out.name}/) | rehearsal | {held['machine']} | {sha[:7]} |" in index
    assert "| ended | 4 |" in index

    # The raw traces stay out of git; the rest of the run is committed with the experiment.
    git(place, "add", "-A")
    tracked = git(place, "ls-files").splitlines()
    assert f"{EXPERIMENT}/runs/{out.name}/manifest.json" in tracked
    assert not [t for t in tracked if "/data/raw/" in t]


def test_each_rehearsal_has_its_own_folder_and_a_failed_one_says_so(fake: FakeInstruCore, tmp_path: Path) -> None:
    place, folder, sha = project(tmp_path, fake.url)
    first = run(folder, "signal-bench-sim")
    (folder / "inputs" / "sweep.json").write_text('{"frequencies_hz": [300]}\n')  # changed, not committed
    second = run(folder, "signal-bench-sim", "the fit diverged")
    assert first.returncode == 0 and second.returncode == 1 and "RuntimeError: the fit diverged" in second.stderr
    folders = sorted((folder / "runs").iterdir())
    assert len(folders) == 2
    manifests = {p.name: json.loads((p / "manifest.json").read_text()) for p in folders}
    failed = manifests[Path(second.stdout.strip()).name]
    assert (failed["status"], failed["dirty"], failed["lab"]) == ("failed", True, None)
    assert failed["inputs"][0]["blob"] == runs.blob_id(b'{"frequencies_hz": [300]}\n')
    assert json.loads((folder / "changes.json").read_text())["run"] == failed["run"]  # the latest rehearsal's
    index = (place / "experiments" / "INDEX.md").read_text()
    assert "| 2 rehearsals |" in index and f"| {sha[:7]}* |" in index and "| failed | 2 |" in index


def test_no_run_folder_when_asked(fake: FakeInstruCore, tmp_path: Path) -> None:
    place, folder, _ = project(tmp_path, fake.url)
    done = run(folder, "signal-bench-sim", QUPAI_LAB_RUNS="0")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "None" and not (folder / "runs").exists()
    assert sorted(p.name for p in (folder / "data").iterdir()) == ["raw", "response.csv"]  # as given, from here
    assert not (place / "experiments" / "INDEX.md").exists()


def test_a_script_that_makes_no_call_leaves_no_folder(fake: FakeInstruCore, tmp_path: Path) -> None:
    _, folder, _ = project(tmp_path, fake.url, "from qupai_lab import Lab\nlab = Lab.from_env(record='calls.jsonl')\n"
                                               "print(lab.out.parent.name, lab.input('sweep.json'))\n")
    done = run(folder)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "runs {'frequencies_hz': [100, 1000]}" and not (folder / "runs").exists()


def test_a_missing_input_is_named(fake: FakeInstruCore, tmp_path: Path) -> None:
    _, folder, _ = project(tmp_path, fake.url, "from qupai_lab import Lab\nLab.from_env().input('nope.json')\n")
    done = run(folder)
    assert done.returncode == 1 and "NO_INPUT" in done.stderr and "nope.json" in done.stderr


def test_the_index_lists_experiments_newest_first(tmp_path: Path) -> None:
    experiments = tmp_path / "experiments"
    for name, started in (("2026-11-20-a", "2026-11-20T10:00:00.000Z"), ("2026-11-22-b", "2026-11-22T09:30:00.000Z")):
        (experiments / name / "runs" / "r1").mkdir(parents=True)
        (experiments / name / "runs" / "r1" / "manifest.json").write_text(json.dumps({
            "run": "r1", "kind": "live", "status": "ended", "exit_code": 0, "commit": "abcdef0123", "dirty": False,
            "started_at": started, "calls": 7, "machines": [{"requested": "bench", "machine": "bench", "held": True}]}))
    (experiments / "2026-11-23-c").mkdir()  # not run yet
    (experiments / "2026-11-20-a" / "runs" / "01a1ff07-old-job").mkdir()  # a run from before manifests
    path = runs.write_index(experiments)
    text = path.read_text()
    assert "| 2026-11-20 | [a](2026-11-20-a/) | 1 live · 1 without a manifest |" in text
    assert "| [01a1ff07-old-job](2026-11-20-a/runs/01a1ff07-old-job/) | no manifest |" in text
    order = [text.index(f"]({n}/) |") for n in ("2026-11-23-c", "2026-11-22-b", "2026-11-20-a")]
    assert order == sorted(order)
    assert "| 2026-11-23 | [c](2026-11-23-c/) | none |  |" in text
    assert "| [r1](2026-11-22-b/runs/r1/) | live | bench | abcdef0 | 2026-11-22 09:30 | ended, exit 0 | 7 |" in text
    before = path.stat().st_mtime_ns
    assert runs.write_index(experiments) == path and path.stat().st_mtime_ns == before  # unchanged: not rewritten
    assert runs.write_index(tmp_path / "none") is None


@pytest.mark.parametrize(("url", "name"), [("http://172.17.0.1:18399/lab/lab-sim", "lab-sim"),
                                           ("http://door/lab/lab-sim/", "lab-sim"),
                                           ("https://instrucore.local:8443", None), ("http://x/lab", None)])
def test_only_a_listing_is_kept_of_the_labs_address(url: str, name: str | None) -> None:
    assert runs.lab_name(url) == name


def test_the_index_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from qupai_lab.cli import main

    (tmp_path / "experiments" / "2026-11-22-a").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert main(["index"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "experiments" / "INDEX.md")
    assert "| 2026-11-22 | [a](2026-11-22-a/) | none |  |" in (tmp_path / "experiments" / "INDEX.md").read_text()
    monkeypatch.chdir(tmp_path / "experiments")
    assert main(["index"]) == 0
    assert main(["index", str(tmp_path / "nowhere")]) == 1 and "no experiments in" in capsys.readouterr().err


def test_the_index_names_an_experiment_once_with_one_date(tmp_path: Path) -> None:
    experiments = tmp_path / "experiments"
    for name, heading in (("2026-11-22-amplitude-linearity", "# 2026-11-22 amplitude linearity of the bench [1 kHz]"),
                          ("2026-11-21-freq-response", "# 2026-11-21 — Frequency response"),
                          ("2026-11-20-no-readme", None), ("scratch", "# Scratch work")):
        (experiments / name).mkdir(parents=True)
        if heading:
            (experiments / name / "README.md").write_text(heading + "\n")
    text = runs.index_text(experiments)
    assert "| 2026-11-22 | [Amplitude linearity of the bench (1 kHz)](2026-11-22-amplitude-linearity/) | none |" in text
    assert "| 2026-11-21 | [Frequency response](2026-11-21-freq-response/) | none |" in text
    assert "| 2026-11-20 | [no readme](2026-11-20-no-readme/) | none |" in text
    assert "|  | [Scratch work](scratch/) | none |" in text
