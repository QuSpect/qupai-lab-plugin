"""The writes a script made, grouped as a live run's card shows them (LS9): ranges, boxes and fixed arguments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from qupai_lab.record import jsonable

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
