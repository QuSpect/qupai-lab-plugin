from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fake_instrucore import FakeInstruCore

from qupai_lab import Lab, LabError, config


def test_from_env_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUPAI_LAB_URL", "http://door/lab/lab-sim/")
    monkeypatch.setenv("QUPAI_LAB_KEY", "qpk_env")
    (tmp_path / ".env").write_text("QUPAI_LAB_URL=http://elsewhere\nQUPAI_LAB_KEY=qpk_file\n")
    lab = Lab.from_env(record="calls.jsonl")
    assert (lab.url, lab._key) == ("http://door/lab/lab-sim", "qpk_env")
    assert lab.recorder.path == tmp_path / "calls.jsonl"


def test_from_env_reads_the_nearest_dotenv_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("QUPAI_LAB_URL=http://far\nQUPAI_LAB_KEY=qpk_far\n")
    project = tmp_path / "project"
    (project / "experiments" / "2026-11-21-x").mkdir(parents=True)
    (project / ".env").write_text(
        "# the lab project's key\nexport QUPAI_LAB_URL='http://172.17.0.1:18399/lab/lab-sim'\n"
        'QUPAI_LAB_KEY="qpk_near" # issued by QuPai\nOTHER=1\n'
    )
    monkeypatch.chdir(project / "experiments" / "2026-11-21-x")
    lab = Lab.from_env()
    assert (lab.url, lab._key) == ("http://172.17.0.1:18399/lab/lab-sim", "qpk_near")


def test_from_env_mixes_the_environment_and_a_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUPAI_LAB_KEY", "qpk_run")  # a run's key in the environment, the URL in the project
    (tmp_path / ".env").write_text("QUPAI_LAB_URL=http://door/lab/lab-sim\nQUPAI_LAB_KEY=qpk_project\n")
    lab = Lab.from_env()
    assert (lab.url, lab._key) == ("http://door/lab/lab-sim", "qpk_run")


def test_from_env_falls_back_to_the_project_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_env = tmp_path / "workspace" / "project" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text("QUPAI_LAB_URL=http://door/lab/lab-x\nQUPAI_LAB_KEY=qpk_ws\n")
    monkeypatch.setattr(config, "PROJECT_ENV", project_env)
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config, "nearest_dotenv", lambda start=None: None)
    lab = Lab.from_env()
    assert (lab.url, lab._key) == ("http://door/lab/lab-x", "qpk_ws")


def test_from_env_says_what_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "nearest_dotenv", lambda start=None: None)
    monkeypatch.setenv("QUPAI_LAB_URL", "http://door")
    with pytest.raises(LabError) as e:
        Lab.from_env()
    assert e.value.code == "NOT_CONFIGURED" and e.value.detail["missing"] == ["QUPAI_LAB_KEY"]
    assert "QUPAI_LAB_KEY" in e.value.message


git = shutil.which("git")
needs_git = pytest.mark.skipif(git is None, reason="needs git")


def _repo(path: Path) -> str:
    def run(*args: str) -> str:
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout

    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
    return run("rev-parse", "--short", "HEAD").strip()


@needs_git
def test_script_ref_for_a_file_in_a_repository(tmp_path: Path) -> None:
    script = tmp_path / "experiments" / "2026-11-21-x" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print(1)\n")
    sha = _repo(tmp_path)
    assert config.script_ref_for(script) == f"experiments/2026-11-21-x/run.py@{sha}"
    long = tmp_path / ("d" * 200) / ("e" * 150) / "run.py"
    long.parent.mkdir(parents=True)
    long.write_text("")
    ref = config.script_ref_for(long)
    assert ref is not None and len(ref) == 300 and ref.endswith(f"/run.py@{sha}")


def test_script_ref_outside_a_repository(tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text("")
    assert config.script_ref_for(script) is None
    assert config.script_ref_for(None) is None


SCRIPT = textwrap.dedent(
    """
    import sys
    from qupai_lab import Lab
    lab = Lab(sys.argv[1], "qpk_test")
    lab.machines()
    lab.machines()
    """
)


def _run(script: Path, url: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run([sys.executable, str(script), url], check=True, cwd=script.parent, env=env, timeout=60)


@needs_git
def test_every_request_names_the_script_and_its_commit(fake: FakeInstruCore, tmp_path: Path) -> None:
    script = tmp_path / "experiments" / "2026-11-21-x" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text(SCRIPT)
    sha = _repo(tmp_path)
    _run(script, fake.url)
    assert [s.headers.get("X-InstruCore-Script") for s in fake.seen] == [f"experiments/2026-11-21-x/run.py@{sha}"] * 2


def test_no_script_header_outside_a_repository(fake: FakeInstruCore, tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text(SCRIPT)
    _run(script, fake.url)
    assert [s.headers.get("X-InstruCore-Script") for s in fake.seen] == [None, None]
    assert all(s.headers["Authorization"] == "Bearer qpk_test" for s in fake.seen)
