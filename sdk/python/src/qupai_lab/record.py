"""The script's own record of its calls: one JSON line per call, and the files the SDK writes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def jsonable(value: Any) -> Any:
    """``json.dumps`` default: NumPy scalars and arrays (``item``, ``tolist``), sets, paths; else ``str``."""
    for attr in ("tolist", "item"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except (TypeError, ValueError):
                pass
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return str(value)


def dumps(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, default=jsonable, indent=indent)


def ensure_parent(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    if p.parent != Path(""):
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


class Recorder:
    """Appends one JSON line per call to ``path``.

    A call that answered a dict or a list is held back until the next line: when the script saves that same
    object with ``Lab.save``, the line names the file it went to (``file``) instead of repeating the result.
    Anything else (the next call, a refusal, the end of a ``with`` block, ``Lab.close``, the interpreter's exit)
    writes it with its result.
    """

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path is not None else None
        self._pending: tuple[dict[str, Any], Any] | None = None

    def add(self, line: dict[str, Any], result: Any = None, *, has_result: bool = False) -> None:
        if self.path is None:
            return
        self.flush()
        if has_result and isinstance(result, (dict, list)):
            self._pending = (line, result)
            return
        if has_result:
            line["result"] = result
        self._write(line)

    def claim(self, value: Any, file: str) -> None:
        """``value`` was saved to ``file``: if it is the held-back call's result, its line names the file."""
        if self._pending is not None and self._pending[1] is value:
            line, _ = self._pending
            self._pending = None
            line["file"] = file
            self._write(line)

    def flush(self) -> None:
        if self._pending is not None:
            line, result = self._pending
            self._pending = None
            line["result"] = result
            self._write(line)

    def _write(self, line: dict[str, Any]) -> None:
        assert self.path is not None
        ensure_parent(self.path)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(dumps(line) + "\n")
