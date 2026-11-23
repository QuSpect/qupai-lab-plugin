"""A fake InstruCore on http.server, answering the routes the Lab SDK calls in InstruCore's own shapes
(src/instrucore/api/machines.py, api/instruments.py): a live bench and a pool of two simulated copies.

A key in ``run_keys`` is a live run's (InstruCore 0.21.0's ``lab_run`` claim on the bench): it may start a
session on the bench with the queue on, and write there while it holds it.

``/v1/moku/<device>/api/<group>/<operation>`` stands in for InstruCore's copy of a Moku's own HTTP API (the
endpoint for the official moku package): the Moku's answers in its own envelope (``success``, ``data``,
``messages``, ``code``) and a ``Moku-Client-Key`` with the claim. ``prefix`` mounts the lab under a path, as a
QuPai sandbox's door does (``/lab/<listing>``)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ROLES = ("dg822_pro_usb", "tbs1104_usb")
POOL = "signal_bench_sim"
POOL_PUBLIC = "signal-bench-sim"
COPIES = ("signal_bench_sim_1", "signal_bench_sim_2")
MOKU_CLIENT_KEY = "fake-moku-client-key"
MOKU_DESCRIBE = {"hardware": "Moku:Go", "mokuOS": "4.3.0", "proxy_version": 2, "bitstreams": {}}


def _instrument_ids(machine: str) -> dict[str, str]:
    return {r: r for r in ROLES} if machine == "bench" else {r: f"{machine}_{r}" for r in ROLES}


@dataclass
class Seen:
    method: str
    path: str  # without the fake's prefix
    headers: Any  # http.client.HTTPMessage: names are case-insensitive
    body: Any
    full_path: str = ""  # as it came, with the prefix


@dataclass
class Canned:
    method: str
    path: str
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json"
    times: int = 1


class FakeInstruCore:
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix.rstrip("/")
        self.seen: list[Seen] = []
        self.canned: list[Canned] = []
        self.sessions: dict[str, dict[str, Any]] = {}  # copy id → session
        self.requests: dict[str, dict[str, Any]] = {}
        self.polls_until_offer = 0  # > 0: a session request queues, offered after this many looks
        self.start_position = 2  # a queued request's first position; each look moves it up one, to 1
        self.taken = False
        self.run_keys: set[str] = set()  # keys of live runs on the bench
        self.before: dict[tuple[str, str], Callable[[], None]] = {}  # called as that request arrives
        self.moku_refusal: str | None = None  # set: every Moku call is refused with it, in the Moku's envelope
        self.lock = threading.Lock()
        self._n = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}{self.prefix}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> FakeInstruCore:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def respond(self, method: str, path: str, status: int, body: Any, **kw: Any) -> None:
        """The next ``times`` requests to ``method path`` get this answer instead of the fake's own."""
        self.canned.append(Canned(method, path, status, body, **kw))

    def calls(self, method: str | None = None, prefix: str = "") -> list[Seen]:
        return [s for s in self.seen if (method is None or s.method == method) and s.path.startswith(prefix)]

    # ---- the lab ----

    def _machine(self, mid: str) -> dict[str, Any]:
        live = mid == "bench"
        session = self.sessions.get(mid)
        ids = _instrument_ids(mid)
        return {
            "id": mid,
            "public_id": "signal-bench" if live else f"{POOL_PUBLIC}-{mid.rsplit('_', 1)[-1]}",
            "name": "Signal bench" if live else "Signal bench (simulated)",
            "kind": "live" if live else "simulated",
            "pool": None if live else POOL,
            "simulates": None if live else "signal_bench",
            "roles": ids,
            "state": "in_session" if session else "free",
            "session": session,
            "offer": None,
            "queue": [],
            "settings": {"max_s": 1800, "offer_s": 300},
            "instruments": [
                {"role": r, "instrument": i, "class": "function_generator" if r.startswith("dg") else "oscilloscope",
                 "status": "online", "simulated": not live}
                for r, i in ids.items()
            ],
            "status": "online",
        }

    def listing(self) -> dict[str, Any]:
        free = [c for c in COPIES if c not in self.sessions]
        return {
            "machines": [self._machine(m) for m in ("bench", *COPIES)],
            "pools": [{"id": POOL, "public_id": POOL_PUBLIC, "name": "Signal bench (simulated)", "kind": "simulated",
                       "simulates": "signal_bench", "copies": list(COPIES), "free": len(free), "queue": [],
                       "settings": {"max_s": 1800, "offer_s": 300}}],
            "discovery": {},
        }

    def _session(self, copy: str, purpose: str) -> dict[str, Any]:
        self._n += 1
        s = {"session_id": f"s_{self._n:04d}", "machine": copy, "instruments": list(_instrument_ids(copy).values()),
             "client_id": "qupai:t:u", "client_name": "Ada", "purpose": purpose, "project": None,
             "started_at": "2026-11-21T10:00:00.000Z", "expires_in_s": 900.0, "limits": {}}
        self.sessions[copy] = s
        return s

    def route(self, method: str, path: str, body: Any, key: str = "") -> tuple[int, Any]:
        parts = path.split("?")[0].strip("/").split("/")
        if len(parts) < 2 or parts[0] != "v1":
            return 404, _err("NOT_FOUND", "no route")
        rest = parts[1:]
        if method == "GET" and rest == ["machines"]:
            return 200, self.listing()
        if method == "GET" and len(rest) == 2 and rest[0] == "machines":
            mid = _internal(rest[1])
            if mid in ("bench", *COPIES):
                return 200, self._machine(mid)
            return 404, _err("NOT_FOUND", f"机器 {rest[1]} 不存在", resource="machine")
        if method == "GET" and len(rest) == 3 and rest[0] in ("machines", "instruments") and rest[2] == "reference":
            return 200, {"schema": "mhs-reference/0", rest[0][:-1]: rest[1]}
        if method == "POST" and len(rest) == 3 and rest[0] == "machines" and rest[2] == "sessions":
            return self._start(_internal(rest[1]), body or {}, key)
        if method == "POST" and len(rest) == 4 and rest[0] == "machines" and rest[2:] == ["sessions", "end"]:
            mid = _internal(rest[1])
            s = self.sessions.pop(mid, None)
            if s is None:
                return 404, _err("NOT_FOUND", f"机器 {mid} 上没有会话", resource="session")
            return 200, {**s, "reason": "released", "note": (body or {}).get("reason", ""), "restore": {"ok": True}}
        if method == "POST" and len(rest) == 4 and rest[0] == "machines" and rest[2:] == ["sessions", "renew"]:
            s = self.sessions.get(_internal(rest[1]))
            return (200, {**s, "expires_in_s": 1800.0}) if s else (404, _err("NOT_FOUND", "no session"))
        if len(rest) == 2 and rest[0] == "session-requests":
            r = self.requests.get(rest[1])
            if r is None:
                return 404, _err("NOT_FOUND", "会话请求不存在", resource="session_request")
            if method == "DELETE":
                if r["status"] in ("pending", "offered"):
                    r["status"] = "cancelled"
                return 200, r
            if r["status"] == "pending":
                self.polls_until_offer -= 1
                if self.polls_until_offer <= 0:
                    machine = "bench" if r["queue"] == "bench" else COPIES[0]
                    r |= {"status": "offered", "machine": machine, "position": 1, "offer_expires_in_s": 300.0}
                else:
                    r["position"] = max(1, r["position"] - 1)
            return 200, r
        if method == "POST" and len(rest) == 4 and rest[0] == "instruments":
            return self._op(rest[1], rest[2], rest[3], body or {})
        if len(rest) >= 4 and rest[0] == "moku" and rest[2] == "api":
            return self._moku(method, "/".join(rest[3:]), body)
        return 404, _err("NOT_FOUND", f"no route {method} {path}")

    def _moku(self, method: str, op: str, body: Any) -> tuple[int, Any] | tuple[int, Any, dict[str, str]]:
        """A Moku's own answers for the calls the tests make; an operation it does not know is a 404, as on a
        Moku."""
        def ok(data: Any) -> tuple[int, Any]:
            return 200, {"success": True, "data": data, "messages": [], "code": None}

        if self.moku_refusal is not None:
            return 200, {"success": False, "data": None, "messages": [self.moku_refusal], "code": "SESSION_REQUIRED"}
        if (method, op) == ("POST", "moku/claim_ownership"):
            return (*ok("Ownership claimed"), {"Moku-Client-Key": MOKU_CLIENT_KEY})
        if (method, op) == ("POST", "moku/relinquish_ownership"):
            return ok("Ownership relinquished")
        if (method, op) == ("GET", "moku/describe"):
            return ok(MOKU_DESCRIBE)
        if method == "GET" and re.fullmatch(r"moku/platform/\d+", op):
            return ok(f"Platform {op.rsplit('/', 1)[-1]} deployed")
        if (method, op) == ("GET", "mim/get_instruments"):
            return ok(["", "", "", ""])
        if method == "GET" and re.fullmatch(r"slot\d+/\w+", op):
            return ok(f"{op.split('/')[1]} deployed")
        if method == "POST" and op.endswith("/get_data"):
            return ok({"ch1": [0.0, 0.5, 1.0], "ch2": [1.0, 0.5, 0.0], "time": [0.0, 1e-6, 2e-6]})
        if method == "POST" and re.fullmatch(r"slot\d+/\w+/\w+", op):
            return ok({"op": op, "args": body})
        return 404, b""

    def _start(self, target: str, body: dict[str, Any], key: str = "") -> tuple[int, Any]:
        if target not in (POOL, "bench", *COPIES):
            return 404, _err("NOT_FOUND", f"机器 {target} 不存在", resource="machine")
        if target == "bench":
            if body.get("simulated"):
                return 400, _err("KIND_MISMATCH", "bench 是真机，不是仿真池或仿真副本", machine="bench", reason="live")
            if key not in self.run_keys:
                return 403, _err("RUN_REQUIRED", "a live machine is held only for a run the member approved: start "
                                 "the session in the conversation", machine="bench")
        queue = "bench" if target == "bench" else POOL
        mine = next((r for r in self.requests.values()
                     if r["queue"] == queue and r["status"] in ("pending", "offered")), None)
        if mine is not None and mine["status"] == "offered":
            mine["status"] = "granted"
            return 201, self._session(mine["machine"], mine["purpose"])
        if mine is not None:
            return 202, _queued(mine)
        if self.polls_until_offer > 0:
            self._n += 1
            r = {"request_id": f"q_{self._n:04d}", "queue": queue, "machine": None, "client_id": "qupai:t:u",
                 "client_name": "Ada", "purpose": body.get("purpose", ""), "project": None, "status": "pending",
                 "position": self.start_position, "created_at": "2026-11-21T10:00:00.000Z",
                 "offer_expires_in_s": None, "wait_reason": None}
            self.requests[r["request_id"]] = r
            return 202, _queued(r)
        if target == "bench":
            if "bench" in self.sessions:
                return 409, _err("MACHINE_IN_SESSION", "bench 正在使用中", machine="bench")
            session = self._session("bench", body.get("purpose", ""))
            if self.taken:  # a run on the bench its member already holds takes that session
                session["taken"] = True
            return 201, session
        free = [c for c in ((target,) if target in COPIES else COPIES) if c not in self.sessions]
        if not free:
            return 409, _err("MACHINE_IN_SESSION", f"{target} 正在使用中", machine=target)
        return 201, self._session(free[0], body.get("purpose", ""))

    def _op(self, inst: str, kind: str, op: str, args: dict[str, Any]) -> tuple[int, Any]:
        owner = next((m for m in ("bench", *COPIES) if inst in _instrument_ids(m).values()), None)
        if owner is None:
            return 404, _err("NOT_FOUND", f"仪器 {inst} 不存在", resource="instrument")
        self._n += 1
        req_id = f"r{self._n:015d}"
        if kind == "check":
            return 200, {"ok": True, "kind": "write"}
        if kind == "write":
            if owner == "bench" and "bench" not in self.sessions:
                return 403, _err("APPROVAL_REQUIRED", "the holder has not allowed this write", instrument=inst, op=op)
            if owner not in self.sessions:
                return 409, _err("SESSION_REQUIRED", "写操作需要先开始会话", machine=owner)
            return 200, {"result": None, "req_id": req_id, "session_id": self.sessions[owner]["session_id"],
                         "timing": {"queue_ms": 0.02, "exec_ms": 0.03}}
        if op == "acquire":
            result: Any = {"channel": args.get("channel"), "samples": [0.0, 0.5, 1.0, 0.5, 0.0]}
        else:
            result = {"op": op, "args": args}
        session = self.sessions.get(owner)
        return 200, {"result": result, "req_id": req_id, "session_id": session["session_id"] if session else None,
                     "timing": {"queue_ms": 0.02, "exec_ms": 0.03}}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                full = self.path
                path = full[len(fake.prefix):] if full.startswith(fake.prefix + "/") else full
                hook = fake.before.get((self.command, path))
                if hook is not None:
                    hook()
                with fake.lock:
                    fake.seen.append(Seen(self.command, path, self.headers, body, full))
                    canned = next((c for c in fake.canned if c.method == self.command and c.path == path), None)
                    if canned is not None:
                        canned.times -= 1
                        if canned.times <= 0:
                            fake.canned.remove(canned)
                        status, out, headers, ctype = canned.status, canned.body, canned.headers, canned.content_type
                    elif fake.prefix and path == full:
                        status, out = 404, _err("NOT_FOUND", "not under the prefix")
                        headers, ctype = {}, "application/json"
                    else:
                        auth = self.headers.get("Authorization") or ""
                        key = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                        answer = fake.route(self.command, path, body, key)
                        status, out = answer[0], answer[1]
                        headers, ctype = (answer[2] if len(answer) > 2 else {}), "application/json"
                data = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _serve

        return Handler


def _internal(mid: str) -> str:
    if mid == "signal-bench":
        return "bench"
    if mid == POOL_PUBLIC:
        return POOL
    if mid.startswith(POOL_PUBLIC + "-"):
        return f"{POOL}_{mid.rsplit('-', 1)[-1]}"
    return mid


def _err(code: str, message: str, **detail: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail}, "req_id": "e0000000000000001"}


def _queued(r: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"code": "SESSION_PENDING", "message": f"已进入 {r['queue']} 的等待队列",
                      "detail": {"request_id": r["request_id"], "position": r["position"]}}, "request": dict(r)}
