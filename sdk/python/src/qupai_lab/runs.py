"""A run's folder and its manifest, and the experiments' index.

Every execution of an experiment's script through ``Lab.from_env()`` has a folder of its own,
``<experiment>/runs/<id>/``: a live run's id is its job (``QUPAI_LAB_RUN``), a rehearsal's the UTC time it started
and four random hex digits (``20261122-021116-a3f9``), so the folders sort by time. Relative paths given to
``record``, ``Lab.save``, ``Lab.table`` and ``Lab.changes`` are inside it; the experiment's own folder keeps only
what a person writes (``README.md``, ``run.py``, ``analysis.py``, ``inputs/``) and the latest rehearsal's
``changes.json``, which a live run's card reads.

The folder is made at the script's first call or output, with ``manifest.json``: what ran (the experiment, the
script and its git blob, its arguments, the commit and whether the experiment differed from it, each file of
``inputs/`` with its blob), where (each machine held, its kind and session), when, how it ended (``status``:
``running``, ``ended``, ``failed``, ``stopped``; a live run's ``exit_code``), its calls, the files it left, and the
versions; ``official_sdk``, each device it worked with a manufacturer's own SDK (``Machine.moku``), whose calls the
lab records. ``simulated`` is true when every machine it held was a simulated copy.

``experiments/INDEX.md`` lists every experiment and its runs from their manifests (``write_index``); a rehearsal
writes it as it ends, ``qupai-lab run`` after a live run, ``qupai-lab index`` on demand.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from qupai_lab import __version__
from qupai_lab.record import dumps, now_iso

MANIFEST = "manifest.json"
INPUTS = "inputs"
RUNS = "runs"
INDEX = "INDEX.md"
EXPERIMENTS = "experiments"
CHANGES = "changes.json"
INPUTS_MAX = 200  # files of inputs/ a manifest lists
HASH_MAX_BYTES = 64 * 1024 * 1024  # a larger input is listed with its size, without a blob id
OFF_ENV = "QUPAI_LAB_RUNS"  # "0": Lab.from_env() makes no run folder (paths are as given, from the working directory)
STOPPED_CODES = (128 + 2, 128 + 9, 128 + 15)  # SIGINT, SIGKILL, SIGTERM

_uncaught: list[BaseException] = []


def _note_uncaught() -> None:
    """The interpreter's exit after an uncaught exception marks the run failed (``sys.excepthook``, chained)."""
    if getattr(sys.excepthook, "_qupai_lab", False):
        return
    previous = sys.excepthook

    def hook(kind: type[BaseException], exc: BaseException, tb: Any) -> None:
        if not issubclass(kind, (SystemExit, KeyboardInterrupt)):
            _uncaught.append(exc)
        previous(kind, exc, tb)

    hook._qupai_lab = True  # type: ignore[attr-defined]
    sys.excepthook = hook


def new_id(now: datetime | None = None) -> str:
    """A rehearsal's id: ``YYYYMMDD-HHMMSS-xxxx`` (UTC, four random hex digits)."""
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def blob_id(data: bytes) -> str:
    """Git's blob id of ``data`` (``git hash-object``)."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _git(folder: Path, *args: str) -> str | None:
    """Git's output (stripped, possibly empty), or None when git failed or is missing."""
    try:
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=10,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def lab_name(url: str) -> str | None:
    """The lab's listing when ``url`` is a sandbox door's (``…/lab/<listing>``); else None (no address is kept)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[-1] if len(parts) >= 2 and parts[-2] == "lab" else None


def main_script() -> Path | None:
    """The script this process runs (``python run.py``): a ``.py`` file outside any installed package; None for
    ``python -c``, the interactive interpreter, ``python -m`` of an installed module, or a test runner."""
    path = getattr(sys.modules.get("__main__"), "__file__", None)
    if not path or not str(path).endswith(".py"):
        return None
    file = Path(path).resolve()
    if not file.is_file() or any(p in ("site-packages", "dist-packages") for p in file.parts):
        return None
    return file


def inputs_of(experiment: Path) -> list[dict[str, Any]]:
    """Each file under ``<experiment>/inputs/``, its path from the experiment, size and blob id."""
    folder = experiment / INPUTS
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for file in sorted(p for p in folder.rglob("*") if p.is_file()):
        if len(out) >= INPUTS_MAX:
            break
        entry: dict[str, Any] = {"path": file.relative_to(experiment).as_posix(), "bytes": file.stat().st_size}
        if entry["bytes"] <= HASH_MAX_BYTES:
            try:
                entry["blob"] = blob_id(file.read_bytes())
            except OSError:
                pass
        out.append(entry)
    return out


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _files(folder: Path) -> tuple[list[str], int]:
    """The run's files (from its folder, without the manifest and raw data) and how many raw files it has."""
    kept, raw = [], 0
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name == MANIFEST or p.name.startswith("."):
            continue
        rel = p.relative_to(folder)
        if "raw" in rel.parts[:-1]:
            raw += 1
        else:
            kept.append(rel.as_posix())
    return kept, raw


class RunFolder:
    """``<experiment>/runs/<id>/`` and its manifest, made at the first call or output (``ensure``)."""

    def __init__(self, experiment: Path, run_id: str, *, live: bool, script: Path | None = None,
                 argv: list[str] | None = None, lab_url: str = "") -> None:
        self.experiment = experiment
        self.id = run_id
        self.live = live
        self.path = experiment / RUNS / run_id
        self.script = script
        self.argv = list(argv or [])
        self.lab = lab_name(lab_url)
        self.manifest: dict[str, Any] | None = None
        _note_uncaught()

    def resolve(self, path: str | os.PathLike[str]) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.path / p

    def ensure(self) -> None:
        if self.manifest is not None:
            return
        self.manifest = self._start()
        self.save()

    def _start(self) -> dict[str, Any]:
        root = _git(self.experiment, "rev-parse", "--show-toplevel")
        commit = _git(self.experiment, "rev-parse", "--verify", "--quiet", "HEAD")
        experiment = str(self.experiment)
        if root:
            try:
                experiment = self.experiment.resolve().relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                pass
        dirty = None
        if root and commit:
            status = _git(self.experiment, "status", "--porcelain", "--untracked-files=all", "--", ".",
                          f":(exclude){RUNS}")
            dirty = None if status is None else bool(status)
        script, blob = None, None
        if self.script is not None:
            try:
                script = self.script.resolve().relative_to(self.experiment.resolve()).as_posix()
            except ValueError:
                script = self.script.name
            try:
                blob = blob_id(self.script.read_bytes())
            except OSError:
                pass
        return {
            "run": self.id,
            "kind": "live" if self.live else "rehearsal",
            "simulated": None,
            "experiment": experiment,
            "script": script,
            "script_blob": blob,
            "argv": self.argv,
            "commit": commit,
            "dirty": dirty,
            "inputs": inputs_of(self.experiment),
            "lab": self.lab,
            "machines": [],
            "official_sdk": [],
            "started_at": now_iso(),
            "ended_at": None,
            "status": "running",
            "exit_code": None,
            "calls": 0,
            "outputs": [],
            "raw_files": 0,
            "versions": {"qupai_lab": __version__, "python": platform.python_version()},
        }

    def save(self) -> None:
        if self.manifest is not None:
            _write_json(self.path / MANIFEST, self.manifest)

    def held(self, *, requested: str, machine: str | None, kind: str | None, session: str | None,
             held: bool) -> int:
        """A machine the script used: held (a session it started) or not (a live machine someone else holds)."""
        self.ensure()
        assert self.manifest is not None
        machines = self.manifest["machines"]
        machines.append({"requested": requested, "machine": machine, "kind": kind, "session": session,
                         "held": held, "held_at": now_iso() if held else None, "released_at": None})
        kinds = [m["kind"] for m in machines if m["held"]]
        self.manifest["simulated"] = all(k != "live" for k in kinds) if kinds else None
        self.save()
        return len(machines) - 1

    def official(self, entry: dict[str, Any]) -> None:
        """A device the script worked with a manufacturer's own SDK (``Machine.moku``): ``package``, ``version``,
        ``machine``, ``device``. The lab records that SDK's calls; ``calls`` counts only the Lab SDK's."""
        self.ensure()
        assert self.manifest is not None
        used = self.manifest.setdefault("official_sdk", [])
        if entry not in used:
            used.append(dict(entry))
        if entry.get("version"):
            self.manifest["versions"][entry["package"]] = entry["version"]
        self.save()

    def released(self, index: int) -> None:
        if self.manifest is not None and 0 <= index < len(self.manifest["machines"]):
            self.manifest["machines"][index]["released_at"] = now_iso()
            self.save()

    def finish(self, status: str, calls: int) -> None:
        """The script ended: its status, its calls and the files it left. Nothing when it never ran a call."""
        if self.manifest is None:
            return
        self.manifest.update(status=status, ended_at=now_iso(), calls=calls)
        self.manifest["outputs"], self.manifest["raw_files"] = _files(self.path)
        self.save()

    def ending(self, calls: int, stopped: bool) -> None:
        """At the interpreter's exit: failed after an uncaught exception, stopped by a signal, else ended."""
        self.finish("failed" if _uncaught else "stopped" if stopped else "ended", calls)


def finish_live(dest: Path, *, run_id: str, experiment: str, commit: str, machine: str, exit_code: int,
                stopped: bool, error: str | None = None) -> None:
    """``qupai-lab run`` after a live run's script: the manifest the script left in ``dest`` (or a short one when it
    left none) gets the approved commit, the exit code and its status from it; a script that did not end well, the
    last lines of its stderr (``error``)."""
    path = dest / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise TypeError(f"{path} is not a JSON object")
    except (OSError, ValueError, TypeError):
        manifest = {"run": run_id, "kind": "live", "simulated": False, "experiment": experiment, "script": "run.py",
                    "commit": commit, "dirty": False, "lab": None, "argv": [machine],
                    "machines": [{"requested": machine, "machine": machine, "kind": "live", "held": True}],
                    "started_at": None, "calls": None,
                    "versions": {"qupai_lab": __version__, "python": platform.python_version()}}
    # The run's script ran in a clean checkout of the approved commit (whose rehearsal outputs the runner removed).
    manifest.update(run=run_id, kind="live", commit=commit, dirty=False, exit_code=exit_code,
                    status="ended" if exit_code == 0 else "stopped" if stopped or exit_code in STOPPED_CODES
                    else "failed")
    manifest["ended_at"] = manifest.get("ended_at") or now_iso()
    if error:
        manifest["error"] = error
    manifest["outputs"], manifest["raw_files"] = _files(dest)
    _write_json(path, manifest)


# ---- the experiments' index ----

def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


_DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(.*))?$")
_LEADING_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\b[\s:·—–-]*")


def _title(readme: Path) -> str:
    """The README's first heading, without a leading date (the folder's name has it)."""
    try:
        for line in readme.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = _LEADING_DATE.sub("", line[2:].strip())
                return title[:1].upper() + title[1:]
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _named(folder: Path) -> tuple[str, str]:
    """An experiment's date (from ``<yyyy-mm-dd>-<topic>``) and what it is called: its README's title, else its
    topic, else the folder's name."""
    dated = _DATED.match(folder.name)
    date, topic = (dated.group(1), (dated.group(2) or "").replace("-", " ")) if dated else ("", folder.name)
    return date, _title(folder / "README.md") or topic or folder.name


def _when(iso: Any) -> str:
    return str(iso)[:16].replace("T", " ") if iso else ""


def _manifests(experiment: Path) -> list[dict[str, Any]]:
    out = []
    for path in (experiment / RUNS).glob(f"*/{MANIFEST}"):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict):
            m.setdefault("run", path.parent.name)
            out.append(m)
    return sorted(out, key=lambda m: (str(m.get("started_at") or ""), str(m.get("run"))), reverse=True)


def _unrecorded(experiment: Path) -> list[str]:
    """Run folders without a manifest (left by an SDK before 0.3.0, or by hand), by name."""
    folder = experiment / RUNS
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir() if p.is_dir() and not (p / MANIFEST).is_file()), reverse=True)


def _machines(m: dict[str, Any]) -> str:
    held = [x.get("machine") or x.get("requested") for x in m.get("machines") or [] if x.get("held")]
    return ", ".join(dict.fromkeys(str(x) for x in held if x)) or ", ".join(
        dict.fromkeys(str(x.get("requested")) for x in m.get("machines") or [] if x.get("requested")))


def _status(m: dict[str, Any]) -> str:
    status = str(m.get("status") or "")
    return f"{status}, exit {m['exit_code']}" if m.get("exit_code") is not None else status


def index_text(experiments: Path) -> str | None:
    """``INDEX.md`` for the experiments folder, or None when it has no experiment."""
    folders = sorted((p for p in experiments.iterdir() if p.is_dir() and not p.name.startswith(".")),
                     key=lambda p: p.name, reverse=True)
    if not folders:
        return None
    about = ("Written by `qupai-lab index` from each run's `manifest.json`; an edit here is replaced. A rehearsal runs "
             "on simulated copies, a live run on the machine; `*` after a commit: the experiment differed from it.")
    head = ["# Experiments", "", about, "", "| Date | Experiment | Runs | Last run |", "|---|---|---|---|"]
    body: list[str] = []
    for folder in folders:
        runs, bare = _manifests(folder), _unrecorded(folder)
        live = sum(1 for m in runs if m.get("kind") == "live")
        counts = " · ".join(x for x in (f"{live} live" if live else "",
                                         f"{len(runs) - live} rehearsal{'s' if len(runs) - live != 1 else ''}"
                                         if len(runs) - live else "",
                                         f"{len(bare)} without a manifest" if bare else "") if x) or "none"
        last = runs[0] if runs else None
        last_text = f"{_when(last.get('started_at'))} {last.get('kind')}, {_status(last)}" if last else ""
        date, name = _named(folder)
        link = f"[{_cell(name).replace('[', '(').replace(']', ')')}]({folder.name}/)"
        head.append(f"| {date} | {link} | {counts} | {_cell(last_text)} |")
        if not runs and not bare:
            continue
        body += ["", f"## {link}" + (f" · {date}" if date else ""), "",
                 "| Run | Kind | Machine | Commit | Started (UTC) | Status | Calls |",
                 "|---|---|---|---|---|---|---|"]
        for m in runs:
            run = str(m.get("run"))
            commit = str(m.get("commit") or "")[:7] + ("*" if m.get("dirty") else "")
            body.append(f"| [{_cell(run[:20])}]({folder.name}/{RUNS}/{run}/) | {_cell(m.get('kind'))} | "
                        f"{_cell(_machines(m))} | {_cell(commit)} | {_when(m.get('started_at'))} | "
                        f"{_cell(_status(m))} | {_cell(m.get('calls'))} |")
        body += [f"| [{_cell(name[:20])}]({folder.name}/{RUNS}/{name}/) | no manifest |  |  |  |  |  |" for name in bare]
    return "\n".join(head + body) + "\n"


def write_index(experiments: Path) -> Path | None:
    """Writes ``<experiments>/INDEX.md`` when its text changed; answers its path, or None without experiments."""
    if not experiments.is_dir():
        return None
    text = index_text(experiments)
    if text is None:
        return None
    path = experiments / INDEX
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")
    return path


def index_folder(experiment: Path) -> Path | None:
    """The experiments folder an experiment is in (its parent named ``experiments``), else None."""
    parent = experiment.resolve().parent
    return parent if parent.name == EXPERIMENTS else None
