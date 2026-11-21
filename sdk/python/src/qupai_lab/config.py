"""Where a script finds its lab (the environment, a ``.env``) and how it names itself (``X-InstruCore-Script``)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from qupai_lab.errors import LabError

URL_ENV = "QUPAI_LAB_URL"
KEY_ENV = "QUPAI_LAB_KEY"
# Set by ``qupai-lab run`` for the script of a live run: the run's id and the live machine it holds.
RUN_ENV = "QUPAI_LAB_RUN"
RUN_MACHINE_ENV = "QUPAI_LAB_RUN_MACHINE"
# A QuPai lab project's checkout in the sandbox: its .env holds the project key.
PROJECT_ENV = Path("/workspace/project/.env")
SCRIPT_REF_MAX = 300


def read_dotenv(path: Path) -> dict[str, str]:
    """``KEY=value`` lines of a ``.env`` file: ``export``, quotes and ``#`` comments as shells write them."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name, value = name.strip(), value.strip()
        if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:
            value = value[1 : value.find(value[0], 1)]  # a quoted value; anything after it is a comment
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[name] = value
    return out


def nearest_dotenv(start: Path | None = None) -> Path | None:
    """The nearest ``.env`` from ``start`` (the working directory) up to the root."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    return None


def settings() -> tuple[str, str]:
    """``QUPAI_LAB_URL`` and ``QUPAI_LAB_KEY``: the environment first, then the nearest ``.env`` up from the
    working directory, then ``/workspace/project/.env``."""
    found = {URL_ENV: os.environ.get(URL_ENV, "").strip(), KEY_ENV: os.environ.get(KEY_ENV, "").strip()}
    nearest = nearest_dotenv()
    looked = ["the environment", str(nearest) if nearest else f"no .env from {Path.cwd()} up", str(PROJECT_ENV)]
    for path in (nearest, PROJECT_ENV):
        if all(found.values()):
            break
        if path is None or not path.is_file():
            continue
        values = read_dotenv(path)
        for name in found:
            found[name] = found[name] or values.get(name, "").strip()
    missing = [name for name, value in found.items() if not value]
    if missing:
        raise LabError(
            "NOT_CONFIGURED",
            f"{' and '.join(missing)} not set; looked in: {'; '.join(looked)}. In a QuPai lab project the "
            "project's .env holds both; at a lab PC, use Lab(url, key)",
            {"missing": missing, "looked": looked},
        )
    return found[URL_ENV], found[KEY_ENV]


def run() -> tuple[str | None, str | None]:
    """Inside a live run: its id (``QUPAI_LAB_RUN``) and its machine (``QUPAI_LAB_RUN_MACHINE``); else None."""
    return (os.environ.get(RUN_ENV, "").strip() or None, os.environ.get(RUN_MACHINE_ENV, "").strip() or None)


def _git(folder: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def script_ref_for(path: str | os.PathLike[str] | None) -> str | None:
    """``<path relative to the git root>@<HEAD short sha>`` for the file at ``path``; None outside a git
    repository, before its first commit, or without git."""
    if not path:
        return None
    file = Path(path).resolve()
    if not file.is_file():
        return None
    root = _git(file.parent, "rev-parse", "--show-toplevel")
    commit = _git(file.parent, "rev-parse", "--short", "HEAD")
    if root is None or commit is None:
        return None
    try:
        rel = file.relative_to(Path(root).resolve())
    except ValueError:
        return None
    ref = f"{rel.as_posix()}@{commit}"
    if not ref.isascii():  # HTTP headers are Latin-1: a non-ASCII file name goes percent-encoded
        ref = quote(ref, safe="/@._-+~=,:")
    if len(ref) > SCRIPT_REF_MAX:  # InstruCore keeps at most 300 characters: keep the commit, cut the path
        ref = ref[-SCRIPT_REF_MAX:]
    return ref


_cached: list[str | None] = []


def running_script_ref() -> str | None:
    """The running ``__main__`` file's ref, computed once per process."""
    if not _cached:
        main = sys.modules.get("__main__")
        _cached.append(script_ref_for(getattr(main, "__file__", None)))
    return _cached[0]
