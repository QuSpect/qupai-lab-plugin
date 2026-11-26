"""The writes a script made, grouped as a live run's card shows them (LS9): ranges, boxes and fixed arguments; and
the changes a live run declares without a rehearsal (``declare_changes``), for a lab with no simulated copy."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qupai_lab.errors import LabError
from qupai_lab.record import dumps, jsonable, now_iso

CHANGES = "changes.json"  # an experiment's: what its live run's card reads
# What a live run's card takes (qupai-server lab_runs.go: changesRecord.check, maxLabRunChanges).
MAX_CHANGES = 32
MAX_FIXED = 64
MAX_RANGES = 16
_ROLE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_OP = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
_QUANTITY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_KEYS = frozenset({"role", "op", "fixed", "quantity", "min", "max", "ranges", "calls"})

# Arguments that pick what is written to rather than a value: CH1 and CH2 are two changes, never a range.
SELECTORS = frozenset({"channel"})


@dataclass
class Write:
    role: str
    op: str
    args: dict[str, Any]
    machine: str


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=jsonable)


def group(writes: list[Write]) -> list[dict[str, Any]]:
    """Group writes by role and op.

    Within a group the numeric arguments that took more than one value move: one gives
    ``{quantity, min, max}``, several a box ``{ranges: {quantity: [min, max]}}``. Every other argument is
    ``fixed``. Writes whose other arguments differ (another shape, another channel, another set of names) are
    separate groups of the same role and op, in the order they were first written.
    """
    groups: dict[tuple[str, str, str], list[Write]] = {}
    for w in writes:
        names = sorted(w.args)
        held = {k: v for k, v in w.args.items() if k in SELECTORS or not _numeric(v)}
        groups.setdefault((w.role, w.op, _key([names, held])), []).append(w)
    out: list[dict[str, Any]] = []
    for (role, op, _), members in groups.items():
        first = members[0].args
        moving: dict[str, list[Any]] = {}
        fixed: dict[str, Any] = {}
        for name, value in first.items():
            values = [m.args[name] for m in members]
            if name not in SELECTORS and _numeric(value) and len({float(v) for v in values}) > 1:
                moving[name] = [min(values, key=float), max(values, key=float)]
            else:
                fixed[name] = value
        entry: dict[str, Any] = {"role": role, "op": op, "calls": len(members)}
        if len(moving) == 1:
            (quantity, (low, high)), = moving.items()
            entry |= {"quantity": quantity, "min": low, "max": high}
        elif moving:
            entry["ranges"] = moving
        entry["fixed"] = fixed
        out.append(entry)
    return out


def _number(value: Any) -> bool:
    return _numeric(value) and math.isfinite(value)


def _refuse(i: int, change: Any, why: str) -> LabError:
    what = f"{change.get('role')}.{change.get('op')}" if isinstance(change, dict) else repr(change)
    return LabError("INVALID_CHANGE", f"change {i + 1} ({what}): {why}", {"index": i, "change": change})


def checked(changes: Any) -> list[dict[str, Any]]:
    """The changes of a live run as its card takes them, or ``LabError`` ``INVALID_CHANGE`` saying which and why.

    Each is ``{"role", "op", "fixed"}`` with, when one argument moves, ``"quantity", "min", "max"``, or when several
    do, ``"ranges": {name: [min, max]}``; ``fixed`` holds the arguments it keeps (``{}`` for none). A change with
    nothing moving is allowed exactly as it is for the whole session."""
    if not isinstance(changes, (list, tuple)):
        raise LabError("INVALID_CHANGE", "the changes are a list of {role, op, fixed, quantity, min, max} or "
                       "{role, op, fixed, ranges}", {"changes": repr(changes)})
    if len(changes) > MAX_CHANGES:
        raise LabError("INVALID_CHANGE", f"a live run's card lists at most {MAX_CHANGES} changes; these are "
                       f"{len(changes)}", {"count": len(changes)})
    out: list[dict[str, Any]] = []
    for i, c in enumerate(changes):
        if not isinstance(c, dict):
            raise _refuse(i, c, "a change is an object {role, op, fixed, …}")
        unknown = sorted(set(c) - _KEYS)
        if unknown:
            hint = "; the arguments it keeps go in fixed" if "args" in unknown or "arguments" in unknown else ""
            raise _refuse(i, c, f"unknown {', '.join(unknown)}{hint}")
        role, op = c.get("role"), c.get("op")
        if not isinstance(role, str) or not _ROLE.fullmatch(role):
            raise _refuse(i, c, "role is the instrument's role on the machine, as machines lists it")
        if not isinstance(op, str) or not _OP.fullmatch(op):
            raise _refuse(i, c, "op is the write op's name, as describe lists it")
        fixed = c.get("fixed", {})
        if not isinstance(fixed, dict) or len(fixed) > MAX_FIXED or not all(isinstance(k, str) for k in fixed):
            raise _refuse(i, c, f"fixed is an object of at most {MAX_FIXED} arguments")
        entry: dict[str, Any] = {"role": role, "op": op}
        calls = c.get("calls")
        if calls is not None:
            if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
                raise _refuse(i, c, "calls is a whole number, 0 or more")
            entry["calls"] = calls
        quantity, ranges = c.get("quantity"), c.get("ranges")
        if quantity is not None and ranges is not None:
            raise _refuse(i, c, "one moving argument is quantity, min and max; several are ranges, not both")
        moving: list[str] = []
        if quantity is not None or "min" in c or "max" in c:
            low, high = c.get("min"), c.get("max")
            if not isinstance(quantity, str) or not _QUANTITY.fullmatch(quantity):
                raise _refuse(i, c, "quantity is the name of the argument that moves")
            if not (_number(low) and _number(high)) or low > high:
                raise _refuse(i, c, "a range is min and max, finite numbers with min <= max")
            entry |= {"quantity": quantity, "min": low, "max": high}
            moving = [quantity]
        elif ranges is not None:
            if not isinstance(ranges, dict) or not ranges or len(ranges) > MAX_RANGES:
                raise _refuse(i, c, f"ranges is an object of 1 to {MAX_RANGES} arguments, each [min, max]")
            box: dict[str, list[Any]] = {}
            for name, r in ranges.items():
                if not isinstance(name, str) or not _QUANTITY.fullmatch(name):
                    raise _refuse(i, c, f"{name!r} in ranges is not an argument's name")
                if not isinstance(r, (list, tuple)) or len(r) != 2 or not all(_number(v) for v in r) or r[0] > r[1]:
                    raise _refuse(i, c, f"ranges.{name} is [min, max], finite numbers with min <= max")
                box[name] = [r[0], r[1]]
            entry["ranges"] = box
            moving = list(box)
        both = sorted(set(moving) & set(fixed))
        if both:
            raise _refuse(i, c, f"{', '.join(both)} both moves and is fixed")
        entry["fixed"] = json.loads(dumps(fixed))
        out.append(entry)
    return out


def declare_changes(experiment: str | os.PathLike[str], changes: Any) -> dict[str, Any]:
    """Declares the writes an experiment's live run will make, without a rehearsal: for a lab with no simulated
    copy of the machine, where nothing can be rehearsed. Writes ``<experiment>/changes.json``, the file a live run's
    card reads, as ``{"declared": true, "written_at", "changes"}`` (each change as ``checked`` takes it), and
    answers it. Commit it with the experiment before asking for the run: the card shows each change marked declared,
    not rehearsed, with the lab's answer for it, and in the run the lab allows exactly these and refuses any other
    write (``APPROVAL_REQUIRED``). A later rehearsal's ``lab.changes()`` replaces the file, and so does a later
    declaration."""
    folder = Path(experiment)
    if not folder.is_dir():
        raise LabError("NOT_FOUND", f"no experiment folder {os.fspath(experiment)!r}", {"experiment": os.fspath(experiment)})
    out = {"declared": True, "written_at": now_iso(), "changes": checked(changes)}
    (folder / CHANGES).write_text(dumps(out, indent=2) + "\n", encoding="utf-8")
    return out
