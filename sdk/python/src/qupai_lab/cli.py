"""``qupai-lab machines``: the lab's machines, the check that this sandbox (or PC) reaches its lab."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from qupai_lab import __version__, config
from qupai_lab.errors import LabError
from qupai_lab.lab import Lab, listed


def _instruments(roles: dict[str, str]) -> str:
    return ", ".join(role if role == inst else f"{role}={inst}" for role, inst in roles.items()) or "-"


def _state(m: dict[str, Any]) -> str:
    status = m.get("status")
    return f"{m.get('state')} ({status})" if status and status != "online" else str(m.get("state"))


def rows(data: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """The listing as (id, kind, state, instruments) rows: live machines, then each pool and its copies."""
    machines = listed(data)
    out = [(m["id"], "live", _state(m), _instruments(m["roles"])) for m in machines if m["kind"] == "live"]
    out += [
        (m["id"], str(m["kind"]), _state(m), _instruments(m["roles"]))
        for m in machines
        if m["kind"] != "live" and not m["pool"]
    ]
    for p in data.get("pools") or []:
        pid = p.get("public_id") or p["id"]
        copies = [m for m in machines if m["pool"] in (pid, p["id"])]
        out.append((pid, "pool", f"{p.get('free', '?')} of {len(p.get('copies') or copies)} free", ""))
        out += [(f"  {m['id']}", "simulated", _state(m), _instruments(m["roles"])) for m in copies]
    return out


def _print_machines(lab: Lab, as_json: bool) -> None:
    data = lab.request("GET", "/v1/machines")
    if as_json:
        print(json.dumps({"lab": lab.url, "machines": listed(data), "pools": data.get("pools") or []}, indent=2,
                         ensure_ascii=False))
        return
    table = [("MACHINE", "KIND", "STATE", "INSTRUMENTS BY ROLE"), *rows(data)]
    widths = [max(len(r[i]) for r in table) for i in range(3)]
    print(f"Lab {lab.url}")
    for r in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(r[:3], widths)) + "  " + r[3])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qupai-lab", description="The QuPai Lab SDK's command line.")
    parser.add_argument("--version", action="version", version=f"qupai-lab {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    p = sub.add_parser(
        "machines",
        help="list the lab's machines: the check that this sandbox reaches its lab",
        description="Lists the lab's machines (id, kind, state, instruments by role) from QUPAI_LAB_URL with "
        "QUPAI_LAB_KEY, read from the environment, the nearest .env up from here, or /workspace/project/.env.",
    )
    p.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    if args.command != "machines":
        parser.print_help(sys.stderr)
        return 2
    try:
        url, key = config.settings()
        _print_machines(Lab(url, key, script_ref=""), args.json)
    except LabError as e:
        print(f"qupai-lab: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
