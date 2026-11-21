"""The Lab SDK over InstruCore's REST API: machines, sessions on simulated copies and on a run's live machine,
instruments by role."""

from __future__ import annotations

import atexit
import csv
import email.utils
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from types import TracebackType
from typing import Any
from urllib.parse import quote

from qupai_lab import __version__, config, metric, stop
from qupai_lab import changes as _changes
from qupai_lab.errors import LabError, from_response
from qupai_lab.record import Recorder, dumps, ensure_parent, now_iso

RETRIES_429 = 5
RETRY_AFTER_DEFAULT_S = 2.0
TIMEOUT_S = 150.0
POLL_S = 2.0
CALLS_EVERY_S = 5.0  # inside a run, the ``calls`` point at most this often
_GONE = ("cancelled", "expired")  # a queue request that ended this way is never offered


def _seg(value: Any) -> str:
    """One path segment, escaped: an id or an op cannot turn into another route."""
    return quote(str(value), safe="")


def _retry_after(value: str | None) -> float:
    """Seconds to wait from a ``Retry-After`` header (seconds or an HTTP date); 2 s without one."""
    if not value:
        return RETRY_AFTER_DEFAULT_S
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT_S
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _say(text: str) -> None:
    print(f"qupai-lab: {text}", file=sys.stderr, flush=True)


class _Answer:
    def __init__(self, status: int, data: Any, headers: Any) -> None:
        self.status = status
        self.data = data
        self.headers = headers


class Lab:
    """One lab, reached at ``url`` with ``key``.

    In a QuPai lab project ``url`` is the lab's address at the sandbox door and ``key`` the project key
    (``Lab.from_env()`` reads both); at a lab PC, InstruCore's own address and an ``ick_`` key.

    ``record``: a file that gets one JSON line per call, appended. ``script_ref``: what the
    ``X-InstruCore-Script`` header says; by default the running script's path in its git repository and the
    commit (``experiments/…/run.py@3f2a91c``), computed once and left out outside a repository; ``""`` sends none.
    """

    def __init__(
        self,
        url: str,
        key: str,
        *,
        record: str | os.PathLike[str] | None = None,
        script_ref: str | None = None,
    ) -> None:
        if not url or not key:
            raise LabError("NOT_CONFIGURED", "Lab(url, key) needs both the lab's address and a key")
        self.url = url.rstrip("/")
        self._key = key
        ref = config.running_script_ref() if script_ref is None else script_ref
        self.script_ref: str | None = ref[-config.SCRIPT_REF_MAX :] if ref else None
        self.recorder = Recorder(os.path.abspath(record) if record is not None else None)
        self.timeout_s = TIMEOUT_S
        self.poll_s = POLL_S
        self._sleep: Callable[[float], None] = time.sleep
        self._writes: list[_changes.Write] = []
        self._machines_used: list[str] = []
        # Inside a live run (``qupai-lab run``): its id, and the live machine ``machine()`` holds for it.
        self.run_id, self.run_machine = config.run()
        self._calls = 0
        self._calls_told = 0
        self._calls_at = float("-inf")
        atexit.register(self._at_exit)

    @classmethod
    def from_env(cls, record: str | os.PathLike[str] | None = None) -> Lab:
        """``QUPAI_LAB_URL`` and ``QUPAI_LAB_KEY`` from the environment, else from the nearest ``.env`` up from
        the working directory, else from ``/workspace/project/.env``."""
        url, key = config.settings()
        return cls(url, key, record=record)

    def __repr__(self) -> str:
        return f"Lab({self.url!r})"

    def __enter__(self) -> Lab:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Writes the record's last line. A Lab keeps no connection open."""
        self.recorder.flush()
        self._tell_calls()

    def _at_exit(self) -> None:
        self.recorder.flush()
        self._tell_calls()

    # ---- a live run's points (QUPAI_METRIC) ----

    def _called(self) -> None:
        self._calls += 1
        if self.run_id and time.monotonic() - self._calls_at >= CALLS_EVERY_S:
            self._tell_calls()

    def _tell_calls(self) -> None:
        if self.run_id and self._calls != self._calls_told:
            metric.emit("calls", self._calls)
            self._calls_told, self._calls_at = self._calls, time.monotonic()

    def _is_run_machine(self, m: dict[str, Any]) -> bool:
        return bool(self.run_machine) and self.run_machine in (m.get("id"), m.get("public_id"))

    # ---- HTTP ----

    def _headers(self, body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": f"qupai-lab/{__version__}",
        }
        if body:
            headers["Content-Type"] = "application/json"
        if self.script_ref:
            headers["X-InstruCore-Script"] = self.script_ref
        return headers

    def _send(self, method: str, path: str, body: dict[str, Any] | None) -> _Answer:
        data = dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self._headers(data is not None))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw, status, headers = resp.read(), resp.status, resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp is not None else b""
            status, headers = e.code, e.headers
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise LabError("UNREACHABLE", f"the lab at {self.url} is unreachable: {reason}") from e
        if status >= 400:
            err = from_response(status, raw, headers.get("Content-Type", "") if headers else "")
            err._headers = headers  # type: ignore[attr-defined]
            raise err
        text = raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(text) if text.strip() else None
        except ValueError:
            parsed = text
        return _Answer(status, parsed, headers)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        note: dict[str, Any] | None = None,
        record: bool = True,
        unwrap: bool = False,
    ) -> Any:
        """Calls a route of the lab (``/v1/…``) and answers its JSON; a refusal raises ``LabError``.

        A 429 waits for ``Retry-After`` (2 s without one) and asks again, up to 5 times. Each call is one line
        of the record; ``note`` adds fields to it. ``unwrap``: answer an op call's ``result``."""
        return self._request(method, path, body, note=note, record=record, unwrap=unwrap).data

    def _line(self, method: str, path: str, body: Any, note: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"time": now_iso(), "method": method, "path": path, **(note or {}), "args": body}

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        note: dict[str, Any] | None = None,
        record: bool = True,
        unwrap: bool = False,
    ) -> _Answer:
        line = self._line(method, path, body, note)
        retries = 0
        while True:
            try:
                answer = self._send(method, path, body)
                break
            except LabError as e:
                headers = getattr(e, "_headers", None)
                if e.status == 429 and retries < RETRIES_429:
                    retries += 1
                    self._sleep(_retry_after(headers.get("Retry-After") if headers else None))
                    continue
                if record:
                    req_id = e.detail.get("req_id") or (headers.get("X-Request-ID") if headers else None)
                    line |= {"ok": False, "status": e.status, "code": e.code, "message": e.message, "req_id": req_id}
                    self.recorder.add(line | ({"retries": retries} if retries else {}))
                raise
        data = answer.data
        req_id = data.get("req_id") if isinstance(data, dict) else None
        if unwrap and isinstance(data, dict) and "result" in data:
            answer.data = data["result"]
        if record:
            self._record_ok(line, answer, req_id, retries)
        return answer

    def _record_ok(self, line: dict[str, Any], answer: _Answer, req_id: str | None = None, retries: int = 0) -> None:
        req_id = req_id or (answer.headers.get("X-Request-ID") if answer.headers else None)
        line |= {"ok": True, "status": answer.status, "code": None, "req_id": req_id}
        self.recorder.add(line | ({"retries": retries} if retries else {}), answer.data, has_result=True)

    # ---- machines ----

    def machines(self) -> list[dict[str, Any]]:
        """The lab's machines: each live machine and each simulated copy, with ``id`` (its public id),
        ``internal_id``, ``name``, ``kind`` (``live`` or ``simulated``), ``state``, ``status`` (health),
        ``pool`` (a copy's pool, by its public id) and ``roles`` (role → instrument id)."""
        return listed(self.request("GET", "/v1/machines"))

    def machine(self, machine_id: str, *, purpose: str = "", wait_s: float | None = None) -> Machine:
        """A machine for a ``with`` block.

        A simulated pool or copy is held for the block: a pool gives a free copy, and when none is free the
        block waits in the pool's queue until one is offered (at most ``wait_s`` seconds when given). It is
        released at the block's end, also when the block raises or SIGTERM or SIGINT stops the script.

        A live machine is held only inside a live run (``qupai-lab run``), and only the run's own machine
        (``QUPAI_LAB_RUN_MACHINE``): the block waits in the machine's queue for its turn, the same way. Any
        other live machine is not held: the member or the conversation holds it, and the lab refuses what it
        must (``RUN_REQUIRED``, ``APPROVAL_REQUIRED``)."""
        return Machine(self, machine_id, purpose=purpose, wait_s=wait_s)

    # ---- files ----

    def save(self, path: str | os.PathLike[str], value: Any, **meta: Any) -> dict[str, Any]:
        """Writes ``value`` as JSON to ``path`` (making its folders) and answers ``{"file": path, **meta}``, a
        row for ``table``. The record's line for the call that answered ``value`` names this file."""
        ensure_parent(path).write_text(dumps(value) + "\n", encoding="utf-8")
        file = os.fspath(path)
        self.recorder.claim(value, file)
        return {"file": file, **meta}

    def table(self, path: str | os.PathLike[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Writes ``rows`` (dicts) as CSV: the columns in the order they first appear, a list or dict cell as
        JSON. Answers ``{"file": path, "rows": n}``."""
        columns: list[str] = []
        for row in rows:
            columns.extend(k for k in row if k not in columns)
        with ensure_parent(path).open("w", newline="", encoding="utf-8") as f:
            out = csv.DictWriter(f, fieldnames=columns)
            out.writeheader()
            for row in rows:
                out.writerow({k: dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()})
        return {"file": os.fspath(path), "rows": len(rows)}

    def changes(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        """Writes the writes made through this Lab to ``path`` as JSON, grouped by role and op, for a live
        run's card (LS9), and answers it: ``{script, machines, written_at, changes}``. In a change one moving
        numeric argument is ``quantity``, ``min``, ``max``; several are a box, ``ranges``; the rest ``fixed``."""
        out = {
            "script": self.script_ref,
            "machines": list(self._machines_used),
            "written_at": now_iso(),
            "changes": _changes.group(self._writes),
        }
        ensure_parent(path).write_text(dumps(out, indent=2) + "\n", encoding="utf-8")
        return out


def roles_of(m: dict[str, Any]) -> dict[str, str]:
    """role → instrument id of a machine as ``GET /v1/machines`` lists it (``roles``, else ``instruments``)."""
    if isinstance(m.get("roles"), dict) and m["roles"]:
        return {str(r): str(i) for r, i in m["roles"].items()}
    return {str(i["role"]): str(i["instrument"]) for i in m.get("instruments") or [] if "role" in i}


def listed(data: dict[str, Any]) -> list[dict[str, Any]]:
    """``GET /v1/machines`` as ``Lab.machines()`` answers it."""
    pools = {p["id"]: p.get("public_id") or p["id"] for p in data.get("pools") or []}
    return [
        {
            "id": m.get("public_id") or m["id"],
            "internal_id": m["id"],
            "name": m.get("name"),
            "kind": m.get("kind"),
            "state": m.get("state"),
            "status": m.get("status"),
            "pool": pools.get(m.get("pool"), m.get("pool")),
            "roles": roles_of(m),
        }
        for m in data.get("machines") or []
    ]


def _find(items: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    return next((x for x in items if target in (x.get("id"), x.get("public_id"))), None)


class Machine:
    """A machine of the lab, as ``lab.machine(id)`` gives it. ``with`` holds a simulated one, and inside a live
    run the run's live machine."""

    def __init__(self, lab: Lab, target: str, *, purpose: str = "", wait_s: float | None = None) -> None:
        self.lab = lab
        self.target = target
        self.purpose = purpose
        self.wait_s = wait_s
        self.id: str | None = None  # the machine in use: the copy a pool gave, or the machine named
        self.public_id: str | None = None
        self.kind: str | None = None
        self.held = False
        self.session: dict[str, Any] | None = None
        self.roles: dict[str, str] = {}
        self._guarded = False

    def __repr__(self) -> str:
        return f"Machine({self.public_id or self.target!r}, kind={self.kind!r}, held={self.held})"

    # ---- holding ----

    def __enter__(self) -> Machine:
        listing = self.lab.request("GET", "/v1/machines")
        machines, pools = listing.get("machines") or [], listing.get("pools") or []
        pool = _find(pools, self.target)
        machine = None if pool else _find(machines, self.target)
        if pool is None and machine is None:
            known = [p.get("public_id") or p["id"] for p in pools]
            known += [m.get("public_id") or m["id"] for m in machines if not m.get("pool")]
            raise LabError(
                "NOT_FOUND",
                f"the lab has no machine or pool {self.target!r}; it has: {', '.join(known) or 'none'}",
                {"machine": self.target, "known": known},
                404,
            )
        live = machine is not None and machine.get("kind") == "live"
        if live and not self.lab._is_run_machine(machine):
            self._use(machine)  # not held: the member or the conversation holds a live machine
            return self
        self._guarded = stop.hold()  # SIGTERM and SIGINT from here on release what is held
        try:
            self._hold(live)
            held = machine if live else _find(machines, self.id)
            self._use(held or self.lab.request("GET", f"/v1/machines/{_seg(self.id)}"))
        except BaseException:
            try:
                self.release(reason=self._reason("qupai-lab: could not start the block"), quiet=True)
            finally:
                self._unguard()
            raise
        return self

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            ended = "qupai-lab: the script's block raised" if kind else "qupai-lab: the script's block ended"
            self.release(reason=self._reason(ended), quiet=kind is not None)
        finally:
            self._unguard()
            self.lab.recorder.flush()

    def release(self, *, reason: str = "qupai-lab: released", quiet: bool = False) -> dict[str, Any] | None:
        """Ends the held session (``sessions/end``); a copy is reset for the next user. A session that already
        ended (idle, or at its longest time) is not an error. ``quiet``: say it on stderr instead of raising."""
        if not self.held or self.id is None:
            return None
        self.held = False
        try:
            with stop.shielded():  # a signal now waits until the session has ended
                return self.lab.request(
                    "POST", f"/v1/machines/{_seg(self.id)}/sessions/end", {"reason": reason[:256]}
                )
        except LabError as e:
            if e.code == "NOT_FOUND":
                _say(f"the session on {self.public_id or self.id} had already ended")
                return None
            if not quiet:
                raise
            _say(f"could not end the session on {self.public_id or self.id}: {e}")
            return None
        finally:
            if self.lab.run_id:
                self.lab._tell_calls()
                metric.emit("held", 0)

    def renew(self) -> dict[str, Any]:
        """Restarts the held session's time limit, unless someone waits for the machine (``QUEUE_WAITING``).
        A run's session has a fixed time: the lab answers ``RUN_REQUIRED``."""
        if not self.held or self.id is None:
            raise LabError("NOT_HELD", f"{self.target} is not held by this block")
        self.session = self.lab.request("POST", f"/v1/machines/{_seg(self.id)}/sessions/renew", {})
        return self.session

    def _reason(self, otherwise: str) -> str:
        signum = stop.stopping()
        return f"qupai-lab: stopped by {stop.name(signum)}" if signum else otherwise

    def _unguard(self) -> None:
        if self._guarded:
            self._guarded = False
            stop.unhold()

    def _hold(self, live: bool) -> None:
        """``POST sessions`` with the queue on: 201 is the session; 202 is a queue request, looked at until it is
        offered, then asked again (which starts the session within the offer). Leaving early withdraws it.

        A run's live machine is asked for with ``purpose: "run <id>"`` and without ``simulated``: the lab
        holds it for the run's longest time."""
        if live:
            body: dict[str, Any] = {"purpose": f"run {self.lab.run_id or ''}".strip()[:256], "queue": True}
        else:
            purpose = (self.purpose or self.lab.script_ref or "qupai-lab")[:256]
            body = {"purpose": purpose, "queue": True, "simulated": True}
        what = self.target if live else f"a copy of {self.target}"
        path = f"/v1/machines/{_seg(self.target)}/sessions"
        answer = self._ask(path, body)
        if answer.status != 202:
            return
        started = time.monotonic()
        request = (answer.data or {}).get("request") or {}
        rid = request.get("request_id")
        if not rid:
            raise LabError("SESSION_PENDING", f"queued for {self.target} without a request id", answer.data or {})
        self._say_queued(answer.data, request, live)
        position = request.get("position")
        self._tell_position(position)
        try:
            while True:
                if self.wait_s is not None and time.monotonic() - started >= self.wait_s:
                    raise LabError(
                        "WAIT_TIMEOUT",
                        f"{what} was not offered within {self.wait_s:g} s; the request was withdrawn",
                        {"request_id": rid, "machine": self.target},
                    )
                self.lab._sleep(self.lab.poll_s)
                poll = f"/v1/session-requests/{_seg(rid)}"
                looked = self.lab._request("GET", poll, record=False)
                status = (looked.data or {}).get("status")
                if status == "pending":
                    if looked.data.get("position") != position:
                        position = looked.data.get("position")
                        _say(f"waiting for {self.target}: position {position}")
                        self._tell_position(position)
                    continue
                self.lab._record_ok(self.lab._line("GET", poll, None), looked)  # one line when it moves on
                if status in _GONE:
                    raise LabError(
                        f"REQUEST_{str(status).upper()}",
                        f"the queue request for {self.target} was {status} before {what} was offered",
                        {"request_id": rid, "machine": self.target},
                    )
                answer = self._ask(path, body)  # offered: ask again, which starts it
                if answer.status != 202:
                    return
                request = (answer.data or {}).get("request") or {}  # the offer ran out or was withdrawn
                rid = request.get("request_id") or rid
        except BaseException:
            try:
                with stop.shielded():
                    self.lab.request("DELETE", f"/v1/session-requests/{_seg(rid)}")
            except LabError:
                pass
            raise

    def _ask(self, path: str, body: dict[str, Any]) -> _Answer:
        """One ``POST sessions``; a 201 is taken at once, so a signal cannot fall between the start and the
        hold."""
        with stop.shielded():
            answer = self.lab._request("POST", path, body)
            if answer.status != 202:
                self.session = answer.data if isinstance(answer.data, dict) else {}
                self.id = self.session.get("machine") or self.target
                self.held = True
                if self.lab.run_id:
                    metric.emit("held", 1)
        return answer

    def _tell_position(self, position: Any) -> None:
        if self.lab.run_id and isinstance(position, int) and not isinstance(position, bool):
            metric.emit("queue_position", position)

    def _say_queued(self, data: dict[str, Any], request: dict[str, Any], live: bool) -> None:
        detail = ((data or {}).get("error") or {}).get("detail") or {}
        why = " (you hold as many simulated sessions as a member may)" if detail.get("reason") == "member_cap" else ""
        waiting = "waiting for its turn" if live else "waiting for a copy"
        _say(f"queued for {self.target} at position {request.get('position')}{why}; {waiting}")

    def _use(self, m: dict[str, Any]) -> None:
        self.id = m["id"]
        self.public_id = m.get("public_id") or m["id"]
        self.kind = m.get("kind")
        self.roles = roles_of(m)
        if self.public_id not in self.lab._machines_used:
            self.lab._machines_used.append(self.public_id)

    # ---- instruments ----

    def role(self, name: str) -> Instrument:
        """The instrument in role ``name`` on this machine, so one script works on a copy and live."""
        if self.kind is None:
            raise LabError("NOT_HELD", f"use the machine in a block first: with lab.machine({self.target!r}) as m")
        if name not in self.roles:
            raise LabError(
                "UNKNOWN_ROLE",
                f"{self.public_id} has no role {name!r}; its roles: {', '.join(self.roles) or 'none'}",
                {"machine": self.public_id, "role": name, "roles": list(self.roles)},
            )
        return Instrument(self, name, self.roles[name])

    def reference(self) -> dict[str, Any]:
        """The machine's reference document (``mhs-reference/0``): roles, wiring, session settings, and each
        instrument's ops, units and limits."""
        return self.lab.request("GET", f"/v1/machines/{_seg(self.id or self.target)}/reference")


class Instrument:
    """One instrument of a machine, by its role."""

    def __init__(self, machine: Machine, role: str, instrument_id: str) -> None:
        self.machine = machine
        self.role = role
        self.id = instrument_id

    def __repr__(self) -> str:
        return f"Instrument({self.role!r}: {self.id!r} on {self.machine.public_id!r})"

    def _call(self, kind: str, op: str, args: dict[str, Any], unwrap: bool) -> Any:
        note = {"machine": self.machine.public_id, "role": self.role, "instrument": self.id, "kind": kind, "op": op}
        path = f"/v1/instruments/{_seg(self.id)}/{kind}/{_seg(op)}"
        try:
            return self.machine.lab.request("POST", path, args, note=note, unwrap=unwrap)
        finally:
            self.machine.lab._called()

    def read(self, op: str, **args: Any) -> Any:
        """Calls the read op ``op`` and answers its result. Anyone may read."""
        return self._call("read", op, args, unwrap=True)

    def write(self, op: str, **args: Any) -> Any:
        """Calls the write op ``op`` in the machine's session and answers its result (often None). The lab
        checks its limits first: a refusal raises ``LabError`` and nothing reaches the instrument."""
        result = self._call("write", op, args, unwrap=True)
        lab = self.machine.lab
        lab._writes.append(_changes.Write(self.role, op, dict(args), self.machine.public_id or self.machine.target))
        return result

    def check(self, op: str, **args: Any) -> dict[str, Any]:
        """Asks whether a write would pass policy, arguments and limits, without a session or the instrument:
        ``{"ok": true, "kind": "write"}``, or the ``LabError`` the write would get."""
        return self._call("check", op, args, unwrap=False)

    def reference(self) -> dict[str, Any]:
        """The instrument's reference document (``mhs-reference/0``): its ops with units, limits and notes."""
        return self.machine.lab.request("GET", f"/v1/instruments/{_seg(self.id)}/reference")
