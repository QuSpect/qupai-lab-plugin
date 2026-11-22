"""``qupai-lab run``: a live run's script, in its own checkout of the approved commit, with the run's key.

QuPai starts it in the place's repository after the member approved the run, with a key file it wrote there::

    qupai-lab run --key-file .qupai/runs/<run>/key.env --commit <sha> experiments/2026-11-21-freq signal-bench

1. reads ``QUPAI_LAB_URL``, ``QUPAI_LAB_KEY``, ``QUPAI_LAB_RUN`` and ``QUPAI_LAB_RUN_MACHINE`` from the key file and
   deletes it;
2. checks the commit out into ``/tmp/qupai-run-<run>`` (``git worktree add --detach``), without the commit's own
   ``data/``, ``figures/``, ``calls.jsonl`` and ``changes.json`` in the experiment's folder;
3. runs ``python run.py <machine>`` there with those four settings (in place of any other ``QUPAI_*``); its output
   is this command's, ``QUPAI_METRIC`` lines included. The SDK writes the script's outputs into its run folder,
   ``runs/<run>/``, with the run's ``manifest.json`` (``qupai_lab.runs``);
4. copies that folder, and any ``data/``, ``figures/``, ``calls.jsonl`` and ``changes.json`` the script left beside
   ``run.py`` (a script that writes its own files), to ``<experiment>/runs/<run>/`` in the repository; gives the
   manifest the approved commit, the exit code and the status (``ended``, ``failed``, ``stopped``), writing a short
   one when the script left none; rewrites ``experiments/INDEX.md``; and removes the checkout;
5. exits with the script's exit code (128 + the signal's number when a signal ended it).

SIGTERM and SIGINT are passed to the script, which ends its held session and exits; after 20 s it is killed. What it
left is still copied and the checkout removed. A run that cannot start exits with 2.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePath
from types import FrameType
from typing import Any

from qupai_lab import config
from qupai_lab import runs as _runs

GRACE_S = 20.0  # after SIGTERM or SIGINT, how long the script has to end its session before it is killed
OUTPUTS = ("data", "figures", "calls.jsonl", "changes.json")
KEYS = (config.URL_ENV, config.KEY_ENV, config.RUN_ENV, config.RUN_MACHINE_ENV)
CANNOT_START = 2
SIGNALS = tuple(s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)) if s is not None)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RunError(Exception):
    """The run cannot start: a bad key file, no repository, no such commit or no ``run.py``."""


def _say(text: str) -> None:
    print(f"qupai-lab: {text}", file=sys.stderr, flush=True)


def run_root() -> Path:
    return Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


class _Signals:
    """SIGTERM and SIGINT, recorded and passed to the script once it runs; never raised in this process, so
    the copy and the checkout's removal always happen."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self.deadline: float | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._previous: dict[int, Any] = {}

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in SIGNALS:
            self._previous[signum] = signal.signal(signum, self._on)

    def restore(self) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
        self._previous.clear()

    def _on(self, signum: int, _frame: FrameType | None) -> None:
        if self.signum is None:
            self.signum = signum
            self.deadline = time.monotonic() + GRACE_S
            _say(f"{signal.Signals(signum).name}: stopping the script (killed after {GRACE_S:g} s)")
        self._forward(signum)

    def _forward(self, signum: int) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.send_signal(signum)
            except OSError:
                pass

    def started(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        if self.signum is not None:  # it came while the script was starting
            self._forward(self.signum)


def read_key_file(path: Path) -> dict[str, str]:
    """The run's four settings. The file is deleted at once, also when it is incomplete."""
    try:
        if not path.is_file():
            raise RunError(f"no key file at {path}")
        values = config.read_dotenv(path)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            _say(f"could not delete the key file {path}: {e}")
    settings = {k: values.get(k, "").strip() for k in KEYS}
    missing = [k for k, v in settings.items() if not v]
    if missing:
        raise RunError(f"the key file {path} has no {', '.join(missing)}")
    if not _RUN_ID.match(settings[config.RUN_ENV]):
        raise RunError(f"the key file's {config.RUN_ENV} {settings[config.RUN_ENV]!r} is not a run id")
    if path.parent.name == settings[config.RUN_ENV]:  # .qupai/runs/<run>/, now empty
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return settings


def experiment_path(value: str) -> PurePath:
    """The experiment's folder, relative to the repository's root, inside it."""
    p = PurePath(value)
    if p.is_absolute() or p.anchor or not p.parts or ".." in p.parts:
        raise RunError(f"the experiment {value!r} is not a folder inside the repository")
    return p


def _repo_root() -> Path:
    done = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if done.returncode != 0 or not done.stdout.strip():
        raise RunError(f"{Path.cwd()} is not in a git repository")
    return Path(done.stdout.strip())


def _resolve(repo: Path, commit: str) -> str:
    if not commit or commit.startswith("-") or any(c.isspace() for c in commit):
        raise RunError(f"{commit!r} is not a commit")
    done = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if done.returncode != 0 or not done.stdout.strip():
        raise RunError(f"the repository at {repo} has no commit {commit}")
    return done.stdout.strip()


def _remove_worktree(repo: Path, tree: Path) -> None:
    _git(repo, "worktree", "remove", "--force", "--force", str(tree))
    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)
    _git(repo, "worktree", "prune")


def _add_worktree(repo: Path, tree: Path, sha: str) -> None:
    if tree.exists():  # left by a run of the same id that was killed
        _remove_worktree(repo, tree)
    done = _git(repo, "worktree", "add", "--detach", "--quiet", str(tree), sha)
    if done.returncode != 0:
        raise RunError(f"could not check {sha[:12]} out into {tree}: {done.stderr.strip()}")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def copy_outputs(folder: Path, dest: Path) -> list[str]:
    """Copies what exists of ``OUTPUTS`` from ``folder`` into ``dest``; symbolic links are copied as links."""
    kept = []
    for name in OUTPUTS:
        src = folder / name
        if not (src.exists() or src.is_symlink()):
            continue
        dest.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dest / name, symlinks=True, dirs_exist_ok=True)
            kept.append(f"{name}/")
        else:
            shutil.copy2(src, dest / name, follow_symlinks=False)
            kept.append(name)
    return kept


def copy_run(folder: Path, run_id: str, dest: Path) -> list[str]:
    """The run's outputs into ``dest``: its run folder (``runs/<run>/``, which the SDK writes) and what of ``OUTPUTS``
    the script left beside ``run.py``. Answers what was kept, the manifest left out."""
    kept: list[str] = []
    own = folder / _runs.RUNS / run_id
    if own.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(own, dest, symlinks=True, dirs_exist_ok=True)
        kept += sorted(f"{p.name}/" if p.is_dir() else p.name for p in own.iterdir() if p.name != _runs.MANIFEST)
    kept += [k for k in copy_outputs(folder, dest) if k not in kept]
    return kept


def _environment(settings: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUPAI_")}
    env.update(settings)
    env["PYTHONUNBUFFERED"] = "1"  # the script's output reaches the log as it is printed
    return env


def _wait(proc: subprocess.Popen[bytes], signals: _Signals) -> int:
    killed = False
    while True:
        try:
            code = proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if not killed and signals.deadline is not None and time.monotonic() >= signals.deadline:
                _say(f"the script did not end within {GRACE_S:g} s: killed")
                proc.kill()
                killed = True
    return 128 - code if code < 0 else code


def run(key_file: str, commit: str, experiment: str, machine: str) -> int:
    signals = _Signals()
    signals.install()
    try:
        return _run(Path(key_file), commit, experiment, machine, signals)
    except (RunError, OSError) as e:
        _say(f"run: {e}")
        return CANNOT_START
    finally:
        signals.restore()


def _run(key_file: Path, commit: str, experiment: str, machine: str, signals: _Signals) -> int:
    settings = read_key_file(key_file)
    run_id, run_machine = settings[config.RUN_ENV], settings[config.RUN_MACHINE_ENV]
    if run_machine != machine:
        raise RunError(f"the run {run_id} is on {run_machine}, not {machine}")
    folder_rel = experiment_path(experiment)
    repo = _repo_root()
    sha = _resolve(repo, commit)
    tree = run_root() / f"qupai-run-{run_id}"
    _add_worktree(repo, tree, sha)
    try:
        folder = tree / folder_rel
        if not (folder / "run.py").is_file():
            raise RunError(f"{folder_rel.as_posix()}/run.py is not in commit {sha[:12]}")
        for name in OUTPUTS:  # the commit's own (a rehearsal's) are not this run's
            _remove(folder / name)
        if signals.signum is not None:
            return 128 + signals.signum
        _say(f"run {run_id}: {folder_rel.as_posix()}/run.py at {sha[:7]} on {machine}")
        try:
            proc = subprocess.Popen([sys.executable, "run.py", machine], cwd=folder, env=_environment(settings))
        except OSError as e:
            raise RunError(f"could not start {sys.executable}: {e}") from e
        code: int | None = None
        try:
            signals.started(proc)
            code = _wait(proc, signals)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if code is None:  # this process was interrupted while it waited
                code = 128 - proc.returncode if proc.returncode < 0 else proc.returncode
            dest = repo / folder_rel / _runs.RUNS / run_id
            try:
                kept = copy_run(folder, run_id, dest)
            except OSError as e:
                _say(f"run {run_id}: could not copy the outputs to {dest}: {e}")
                kept = None
            if kept is not None:
                try:
                    _runs.finish_live(dest, run_id=run_id, experiment=folder_rel.as_posix(), commit=sha,
                                      machine=machine, exit_code=code, stopped=signals.signum is not None)
                    experiments = _runs.index_folder(repo / folder_rel)
                    if experiments is not None:
                        _runs.write_index(experiments)
                except OSError as e:
                    _say(f"run {run_id}: could not write its manifest or the index: {e}")
        if kept is None:
            return code or 1
        where = f"{folder_rel.as_posix()}/runs/{run_id}/"
        _say(f"run {run_id}: the script exited with {code}; "
             + (f"kept {', '.join(kept)} in {where}" if kept else f"it left no output; its manifest is in {where}"))
        return code
    finally:
        _remove_worktree(repo, tree)
