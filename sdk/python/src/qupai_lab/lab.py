"""The Lab SDK over InstruCore's REST API: machines, sessions on simulated copies and on a run's live machine,
instruments by role."""

from __future__ import annotations

import atexit
import csv
import email.utils
import importlib.metadata
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import quote, urlsplit

from qupai_lab import __version__, config, metric, stop
from qupai_lab import direct as _direct
from qupai_lab import changes as _changes
from qupai_lab.moku_forward import Forwarder
from qupai_lab import runs as _runs
from qupai_lab.errors import LabError, RunSessionEnded, from_response
from qupai_lab.record import Recorder, dumps, ensure_parent, now_iso

RETRIES_429 = 5
RETRY_AFTER_DEFAULT_S = 2.0
TIMEOUT_S = 150.0
POLL_S = 2.0
# A queued request is looked at every POLL_S at first, then less often while its place does not move, up to
# POLL_MAX_S: a run may wait hours, and each look counts against the member's per-minute calls to the lab. An
# offer is kept for minutes (InstruCore's offer_s, 300 s by default), so POLL_MAX_S still starts it in time.
POLL_MAX_S = 15.0
CALLS_EVERY_S = 5.0  # inside a run, the ``calls`` point at most this often
# After the lab refused a call of the official moku package because the run's session ended, how long the package
# has to read that answer before the script is stopped in its main thread: stopping it while the package is still
# reading leaves its connection half used, and its own exit then fails over that instead.
STOP_AFTER_S = 0.5
_GONE = ("cancelled", "expired")  # a queue request that ended this way is never offered
# A refusal that says the caller does not hold the machine (any more): no session of its own there, or someone
# else's. Inside a live run, while the block holds the run's machine, it means the run's session ended.
_NOT_HOLDING = ("BENCH_NOT_HELD", "SESSION_REQUIRED", "BENCH_HELD", "MACHINE_IN_SESSION")
_SLACK_S = 5.0  # how close to a session's longest time or idle limit its end is taken to be the lab's
# A machine's and a Moku's id as InstruCore writes ids (``moku_pro``) and public ids (``moku-bench-sim-1``): they go
# into the official moku package's address unescaped, so they may not carry a ``/``, a ``.`` or anything else that
# changes the route.
_MOKU_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _seg(value: Any) -> str:
    """One path segment, escaped: an id or an op cannot turn into another route."""
    return quote(str(value), safe="")


def _plain_id(value: Any, kind: str, what: str) -> None:
    """Refuses (``INVALID_MACHINE``, ``INVALID_DEVICE``) an id that could change the official package's route."""
    if not isinstance(value, str) or not _MOKU_ID.fullmatch(value):
        raise LabError(
            f"INVALID_{kind.upper()}",
            f"{value!r} is not {what}: letters, digits, '_' and '-', at most 64, as the lab names it",
            {kind: value if isinstance(value, str) else repr(value)},
        )


def moku_target(url: str, machine_id: Any, device_id: Any) -> str:
    """Where the lab answers the Moku ``device_id`` of the machine ``machine_id`` (the live machine, or the simulated
    copy held) in the lab at ``url``: ``<url>/v1/moku/<machine_id>/<device_id>``, InstruCore's copy of the Moku's own
    HTTP API. ``Machine.moku`` points the official package at a local forwarder that passes its calls on to here."""
    _plain_id(machine_id, "machine", "a machine's id")
    _plain_id(device_id, "device", "a Moku's device id")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise LabError(
            "INVALID_URL",
            f"the lab's address {url!r} is not an http(s)://host[:port][/path] address",
            {"url": url},
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}/v1/moku/{machine_id}/{device_id}"


def _installed(package: str) -> str | None:
    """The installed version of ``package``, without importing it; None when it is not installed."""
    try:
        return importlib.metadata.version(package)
    except (importlib.metadata.PackageNotFoundError, ValueError, OSError):
        return None


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


def _parse_time(value: Any) -> datetime | None:
    """An ISO 8601 time as InstruCore writes it (``2026-11-26T05:25:19.123Z``), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _utc(when: datetime) -> str:
    """``2026-11-26 05:25:19 UTC``: a time as a sentence says it."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class _Answer:
    def __init__(self, status: int, data: Any, headers: Any) -> None:
        self.status = status
        self.data = data
        self.headers = headers


class Lab:
    """One lab, reached at ``url`` with ``key``.

    In a QuPai lab project ``url`` is the lab's address at the sandbox door and ``key`` the project key
    (``Lab.from_env()`` reads both); at a lab PC, InstruCore's own address and an ``ick_`` key; outside QuPai, the
    lab's REST address at QuPai's gateway and the person's own key (``Lab.from_qupai(lab)`` finds both).

    ``unix_socket``: a Unix socket that carries every request, whatever host ``url`` names: a lab workstation's
    door (``Lab.from_qupai`` passes it there). ``direct``: the lab is worked directly, as the person themself
    (``Lab.from_qupai``), so ``machine()`` holds a live machine for its block as their own session.

    ``record``: a file that gets one JSON line per call, appended. ``script_ref``: what the
    ``X-InstruCore-Script`` header says; by default the running script's path in its git repository and the
    commit (``experiments/…/run.py@3f2a91c``), computed once and left out outside a repository; ``""`` sends none.

    ``run_folder``: where this execution's outputs go (``Lab.from_env()`` makes one for a script, see ``runs``):
    relative paths given to ``record``, ``save``, ``table`` and ``changes`` are inside it, and its ``manifest.json``
    records the run. Without one, paths are as given, from the working directory.
    """

    def __init__(
        self,
        url: str,
        key: str,
        *,
        record: str | os.PathLike[str] | None = None,
        script_ref: str | None = None,
        run_folder: _runs.RunFolder | None = None,
        unix_socket: str | None = None,
        direct: bool = False,
    ) -> None:
        if not url or not key:
            raise LabError("NOT_CONFIGURED", "Lab(url, key) needs both the lab's address and a key")
        self.url = url.rstrip("/")
        self._key = key
        self.unix_socket = unix_socket or None
        self.direct = direct
        # How a request is sent: straight to the address, or through the workstation's door.
        self._open: _direct.Opener = _direct.socket_opener(unix_socket) if unix_socket else urllib.request.urlopen
        ref = config.running_script_ref() if script_ref is None else script_ref
        self.script_ref: str | None = ref[-config.SCRIPT_REF_MAX :] if ref else None
        self.run_folder = run_folder
        self.recorder = Recorder(self._path(record) if record is not None else None)
        self.timeout_s = TIMEOUT_S
        self.poll_s = POLL_S
        self._sleep: Callable[[float], None] = time.sleep
        self._writes: list[_changes.Write] = []
        self._machines_used: list[str] = []
        # Devices worked with a manufacturer's own SDK (``Machine.moku``): its calls go to the lab, not through here.
        self._official_used: list[dict[str, Any]] = []
        # Inside a live run (``qupai-lab run``): its id, and the live machine ``machine()`` holds for it.
        self.run_id, self.run_machine = config.run()
        # The run's session ended while the script ran, not by the script: every later call raises it again.
        self._ended: RunSessionEnded | None = None
        self._ending = threading.Lock()
        self._active_at = time.monotonic()  # the last call's answer (the lab's idle limit counts from it)
        self._calls = 0
        self._calls_told = 0
        self._calls_at = float("-inf")
        atexit.register(self._at_exit)

    @classmethod
    def from_env(cls, record: str | os.PathLike[str] | None = None, *, runs: bool | None = None) -> Lab:
        """``QUPAI_LAB_URL`` and ``QUPAI_LAB_KEY`` from the environment, else from the nearest ``.env`` up from
        the working directory, else from ``/workspace/project/.env``.

        A script run as ``python run.py`` gets its run folder, ``<the script's folder>/runs/<id>/`` (``runs``):
        a live run's id is its job, a rehearsal's the time it started. ``runs=False``, or ``QUPAI_LAB_RUNS=0``,
        makes none; there is none either for ``python -c``, the interactive interpreter or a test runner."""
        url, key = config.settings()
        return cls._for_script(url, key, record, runs)

    @classmethod
    def from_qupai(cls, lab: str, record: str | os.PathLike[str] | None = None, *, runs: bool | None = None) -> Lab:
        """The lab ``lab`` (its id, as ``qupai lab list`` names it: ``"magnetic"``) outside QuPai, with the person's
        own ``qupai`` sign-in (``qupai login``): ``QUPAI_CREDENTIALS``, else ``qupai/credentials.json`` in the
        user's configuration directory.

        The platform says which labs the person may work directly from where they are (``GET /v1/me/lab-access``:
        QuPai staff, a grant to their account, or a grant to the workstation they sit at), and the lab is then
        InstruCore's REST API at ``<platform>/rest/<listing>``, with the person's key. On a lab workstation every
        call goes through the workstation's door (``QUPAI_WORKSTATION_LAB_SOCKET``, else
        ``/Library/QuPai/workstation/lab.sock`` when it exists). A lab the person may not work directly raises
        ``LabError`` ``NO_DIRECT_ACCESS``, naming the labs they may: for the others, the lab's agent is the way
        (``qupai ask -lab <lab>``). ``record`` and ``runs`` as for ``from_env``.

        ``machine()`` of such a Lab holds a live machine too, as the person's own session (the lab checks their role
        on the bench and its limits), so the same experiment code runs here and as a live run in a QuPai sandbox."""
        found = _direct.route(lab)
        return cls._for_script(found.url, found.key, record, runs, unix_socket=found.socket, direct=True)

    @classmethod
    def _for_script(cls, url: str, key: str, record: str | os.PathLike[str] | None, runs: bool | None,
                    unix_socket: str | None = None, direct: bool = False) -> Lab:
        """A Lab for the running script, with its run folder unless ``runs`` is False (``from_env``)."""
        folder = None
        script = _runs.main_script()
        if runs is None:
            runs = script is not None and os.environ.get(_runs.OFF_ENV, "").strip() != "0"
        if runs:
            experiment = script.parent if script is not None else Path.cwd()
            run_id, _ = config.run()
            folder = _runs.RunFolder(experiment, run_id or _runs.new_id(), live=bool(run_id), script=script,
                                     argv=sys.argv[1:], lab_url=url)
        return cls(url, key, record=record, run_folder=folder, unix_socket=unix_socket, direct=direct)

    # ---- where a run's files go ----

    @property
    def out(self) -> Path | None:
        """This execution's folder (``<experiment>/runs/<id>/``), or None without a run folder."""
        return self.run_folder.path if self.run_folder else None

    @property
    def experiment(self) -> Path:
        """The experiment's folder: the script's, or the working directory without a run folder."""
        return self.run_folder.experiment if self.run_folder else Path.cwd()

    @property
    def inputs(self) -> Path:
        """``<experiment>/inputs/``: the files the script reads (a sweep's values, a configuration). A live run
        has them at its commit; the manifest lists each with its git blob id."""
        return self.experiment / _runs.INPUTS

    def input(self, name: str) -> Any:
        """``inputs/<name>``: parsed when it is JSON (``.json``), else its text."""
        path = self.inputs / name
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise LabError("NO_INPUT", f"no input {name!r}: {path} does not exist", {"input": name}) from None
        return json.loads(text) if path.suffix.lower() == ".json" else text

    def _path(self, path: str | os.PathLike[str]) -> Path:
        """A path the script gave: inside the run folder when relative and there is one."""
        if self.run_folder is not None:
            return self.run_folder.resolve(path)
        return Path(os.path.abspath(path))

    def _ran(self) -> None:
        if self.run_folder is not None:
            self.run_folder.ensure()

    def _used(self, m: Machine, *, held: bool) -> int | None:
        if self.run_folder is None:
            return None
        session = (m.session or {}).get("session_id") if held else None
        return self.run_folder.held(requested=m.target, machine=m.public_id or m.id, kind=m.kind, session=session,
                                    held=held)

    def __repr__(self) -> str:
        return f"Lab({self.url!r}, unix_socket={self.unix_socket!r})" if self.unix_socket else f"Lab({self.url!r})"

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
        folder = self.run_folder
        if folder is None or folder.manifest is None:
            return
        folder.ending(self._calls, stopped=stop.stopping() is not None)
        experiments = _runs.index_folder(folder.experiment)
        if experiments is not None and not folder.live:  # a live run's is written by qupai-lab run, in the repository
            try:
                _runs.write_index(experiments)
            except OSError as e:
                _say(f"could not write {experiments / _runs.INDEX}: {e}")

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

    def check_running(self) -> None:
        """Raises ``RunSessionEnded`` once the run's session ended while the script ran (``Machine``): the run
        stops, and nothing more reaches the lab through this Lab."""
        ended = self._ended
        if ended is not None:
            raise RunSessionEnded(ended.message, ended.detail)

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

    def _official_headers(self, package: str, version: str | None) -> dict[str, str]:
        """The headers of a manufacturer's SDK's calls through this Lab: this Lab's own key and script header (the
        key is what tells the lab whose hold a call is in), without ``Accept: application/json``, since such an SDK
        also fetches files. A new dict each time: the official moku package makes it its HTTP session's headers and
        adds its ``Moku-Client-Key`` to it."""
        headers = self._headers(body=False)
        del headers["Accept"]
        headers["User-Agent"] += f" {package}/{version}" if version else f" {package}"
        return headers

    def _official(self, package: str, version: str | None, m: Machine, device: str) -> None:
        """Notes in the run that ``package`` works ``device`` on ``m``: the lab records each of its calls; the
        record file and ``calls`` here see none of them. Its writes reach ``changes`` from the lab's answers
        (``_official_answer``)."""
        entry = {"package": package, "version": version, "machine": m.public_id or m.id, "device": device}
        if entry in self._official_used:
            return
        self._official_used.append(entry)
        if self.run_folder is not None:
            self.run_folder.official(entry)

    def _official_answer(self, m: Machine, device: str) -> Callable[[int, str, bytes], None]:
        """What the local forwarder of a manufacturer's SDK (``m.moku``) does with each JSON answer of the lab: the
        Moku's envelope lists the writes the lab made (``instrucore.writes``: instrument, op and arguments, as it
        matches a run's permission), and each is kept for ``changes`` under its role on ``m``. A lab whose answers
        list none (InstruCore before 0.26.2) is said once, and ``changes`` notes it: its writes are not there.

        Inside a live run a refusal that says the run does not hold its machine any more (``_NOT_HOLDING``) is
        looked into as for this SDK's own calls (``Machine._lost``): when the run's session ended, the stand-in refuses
        every later call itself, and ``STOP_AFTER_S`` later the script is stopped in its main thread
        (``stop.interrupt``), as by a signal, unless its block ended first (with ``RunSessionEnded``)."""
        roles = {iid: role for role, iid in reversed(list(m.roles.items()))}
        machine = m.public_id or m.target
        said = False

        def record(status: int, content_type: str, content: bytes) -> None:
            nonlocal said
            if status != 200 or not content_type.startswith("application/json"):
                return  # a file the package downloads: never a write
            try:
                body = json.loads(content)
            except ValueError:
                return
            if not isinstance(body, dict):
                return
            self._active_at = time.monotonic()
            if body.get("success") is False and body.get("code") in _NOT_HOLDING:
                messages = body.get("messages") if isinstance(body.get("messages"), list) else []
                ended = m._lost(str(body["code"]), str(messages[0]) if messages else "")
                if ended is not None:  # once the package has read the refusal, the script stops
                    timer = threading.Timer(STOP_AFTER_S, stop.interrupt, (ended,))
                    timer.daemon = True
                    timer.start()
                return
            if body.get("success") is not True:
                return
            listed = body.get("instrucore")
            if not isinstance(listed, dict) or not isinstance(listed.get("writes"), list):
                if not said:
                    said = True
                    for e in self._official_used:
                        if e["machine"] == machine and e["device"] == device:
                            e["writes_listed"] = False
                    _say(f"the lab lists no writes of the {device} calls (InstruCore before 0.26.2): changes.json "
                         "leaves them out, and a live run of this script would be refused at its first one")
                return
            for w in listed["writes"]:
                if not isinstance(w, dict) or not isinstance(w.get("op"), str):
                    continue
                instrument = str(w.get("instrument") or "")
                args = w.get("args") if isinstance(w.get("args"), dict) else {}
                self._writes.append(_changes.Write(roles.get(instrument, instrument), w["op"], dict(args), machine))

        return record

    def _send(self, method: str, path: str, body: dict[str, Any] | None) -> _Answer:
        data = dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self._headers(data is not None))
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                raw, status, headers = resp.read(), resp.status, resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp is not None else b""
            status, headers = e.code, e.headers
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            door = f", through the workstation's door at {self.unix_socket}," if self.unix_socket else ""
            raise LabError("UNREACHABLE", f"the lab at {self.url}{door} is unreachable: {reason}") from e
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
        force: bool = False,
    ) -> _Answer:
        if not force:  # ``force``: a session's end, which a run that stopped still sends for another machine
            self.check_running()
        line = self._line(method, path, body, note)
        if record:
            self._ran()
        retries = 0
        while True:
            try:
                answer = self._send(method, path, body)
                self._active_at = time.monotonic()
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
        must (``RUN_REQUIRED``, ``APPROVAL_REQUIRED``). A lab worked directly (``Lab.from_qupai``) holds any live
        machine for the block, as the person's own session; one they already held when the block began stays
        held at its end."""
        return Machine(self, machine_id, purpose=purpose, wait_s=wait_s)

    # ---- files ----

    def save(self, path: str | os.PathLike[str], value: Any, **meta: Any) -> dict[str, Any]:
        """Writes ``value`` as JSON to ``path`` (making its folders) and answers ``{"file": path, **meta}``, a
        row for ``table``. The record's line for the call that answered ``value`` names this file."""
        self._ran()
        target = self._path(path)
        ensure_parent(target).write_text(dumps(value) + "\n", encoding="utf-8")
        file = os.fspath(path)
        self.recorder.claim(value, file)
        return {"file": file, **meta}

    def table(self, path: str | os.PathLike[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Writes ``rows`` (dicts) as CSV: the columns in the order they first appear, a list or dict cell as
        JSON. Answers ``{"file": path, "rows": n}``."""
        columns: list[str] = []
        for row in rows:
            columns.extend(k for k in row if k not in columns)
        self._ran()
        with ensure_parent(self._path(path)).open("w", newline="", encoding="utf-8") as f:
            out = csv.DictWriter(f, fieldnames=columns)
            out.writeheader()
            for row in rows:
                out.writerow({k: dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()})
        return {"file": os.fspath(path), "rows": len(rows)}

    def changes(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        """Writes the writes made through this Lab to ``path`` as JSON, grouped by role and op, for a live
        run's card (LS9), and answers it: ``{script, machines, written_at, changes}``. In a change one moving
        numeric argument is ``quantity``, ``min``, ``max``; several are a box, ``ranges``; the rest ``fixed``.

        In a rehearsal with a run folder it is also written to ``<experiment>/changes.json``, the file a live
        run's card reads, with ``run`` naming the rehearsal."""
        self._ran()
        out = {
            "script": self.script_ref,
            # run.py's git blob id as it ran: the live run's card says rehearsed when its commit has this file.
            "script_blob": config.running_script_blob(),
            "machines": list(self._machines_used),
            # A device the script worked with a manufacturer's SDK (m.moku): its writes are among the changes when
            # the lab listed them in its answers (InstruCore 0.26.2 on); "writes_listed": false says they are not.
            **({"official_sdk": [dict(e) for e in self._official_used]} if self._official_used else {}),
            "written_at": now_iso(),
            "changes": _changes.group(self._writes),
        }
        folder = self.run_folder
        if folder is not None:
            out = {"run": folder.id, **out}
        target = self._path(path)
        ensure_parent(target).write_text(dumps(out, indent=2) + "\n", encoding="utf-8")
        card = folder.experiment / _runs.CHANGES if folder is not None and not folder.live else None
        if card is not None and card.resolve() != target.resolve():
            card.write_text(dumps(out, indent=2) + "\n", encoding="utf-8")
        return out


def _seconds(value: Any) -> float | None:
    """A positive number of seconds from an answer, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


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
        self._entry: int | None = None  # this block's line in the run's manifest
        self._as_run = False  # the live machine is held for the live run this script is
        self._before: str | None = None  # a lab worked directly: the live machine's session when the block began
        self._forwarders: dict[str, Forwarder] = {}  # m.moku(): one local stand-in per Moku device
        # The session held: since when (as the lab says, and on this clock), its longest time and idle limit.
        self._held_since: datetime | None = None
        self._held_at = 0.0
        self._max_s: float | None = None
        self._idle_s: float | None = None
        self._ended: RunSessionEnded | None = None  # the run's session ended while this block held it

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
        self._as_run = live and self.lab._is_run_machine(machine)
        if live and not self._as_run and not self.lab.direct:
            self._use(machine)  # not held: the member or the conversation holds a live machine
            self.lab._used(self, held=False)
            return self
        if live and not self._as_run:  # worked directly: the person's own session, unless they hold one already
            self._before = ((machine or {}).get("session") or {}).get("session_id")
        self._guarded = stop.hold()  # SIGTERM and SIGINT from here on release what is held
        try:
            self._hold(live)
            held = machine if live else _find(machines, self.id)
            self._use(held or self.lab.request("GET", f"/v1/machines/{_seg(self.id)}"))
            self._entry = self.lab._used(self, held=True)
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
            for forwarder in self._forwarders.values():
                forwarder.close()
            self._forwarders.clear()
            self.lab.recorder.flush()
        # The run's session ended while the block ran: the block never ends as if it had run (a script that caught
        # what a call raised, or the moku package's own exception, leaves it with the reason).
        lost = self._ended
        if lost is not None and not isinstance(exc, RunSessionEnded):
            raise RunSessionEnded(lost.message, lost.detail) from exc

    def release(self, *, reason: str = "qupai-lab: released", quiet: bool = False) -> dict[str, Any] | None:
        """Ends the held session (``sessions/end``); a copy is reset for the next user. A session that already
        ended (idle, or at its longest time) is not an error. ``quiet``: say it on stderr instead of raising."""
        if not self.held or self.id is None:
            return None
        self.held = False
        if self._entry is not None and self.lab.run_folder is not None:
            self.lab.run_folder.released(self._entry)
        if getattr(self, "taken", False):
            whose = "the run used its member's own session" if self._as_run else "it was your session before the block"
            _say(f"{self.public_id or self.id} stays held: {whose}")
            self._tell_held(False)
            return None
        try:
            with stop.shielded():  # a signal now waits until the session has ended
                return self.lab._request(
                    "POST", f"/v1/machines/{_seg(self.id)}/sessions/end", {"reason": reason[:256]}, force=True
                ).data
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
            self._tell_held(False)

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
        holds it for the run's longest time. A lab worked directly asks for any other live machine with the block's
        purpose (the script by default), without ``simulated``."""
        if live and self._as_run:
            body: dict[str, Any] = {"purpose": f"run {self.lab.run_id or ''}".strip()[:256], "queue": True}
        elif live:
            body = {"purpose": (self.purpose or self.lab.script_ref or "qupai-lab")[:256], "queue": True}
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
        still = 0  # looks since the position last moved
        try:
            while True:
                if self.wait_s is not None and time.monotonic() - started >= self.wait_s:
                    raise LabError(
                        "WAIT_TIMEOUT",
                        f"{what} was not offered within {self.wait_s:g} s; the request was withdrawn",
                        {"request_id": rid, "machine": self.target},
                    )
                self.lab._sleep(min(self.lab.poll_s * 2**still, max(POLL_MAX_S, self.lab.poll_s)))
                poll = f"/v1/session-requests/{_seg(rid)}"
                looked = self.lab._request("GET", poll, record=False)
                status = (looked.data or {}).get("status")
                if status == "pending":
                    still = min(still + 1, 8)
                    if looked.data.get("position") != position:
                        still = 0
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
                # A run on the machine its member already holds takes that session (InstruCore's "taken"): it is
                # the member's, so the block leaves it held when it ends.
                self.taken = self.session.get("taken") is True
                # Worked directly: a session the person held before the block began (in the conversation, say) is
                # theirs, and stays held when the block ends.
                if self._before and self.session.get("session_id") == self._before:
                    self.taken = True
                self._held_at = time.monotonic()
                self._held_since = _parse_time(self.session.get("started_at"))
                self._max_s = _seconds(self.session.get("max_expires_in_s"))
                self._idle_s = _seconds(self.session.get("idle_expires_in_s"))
                self._tell_held(True)
        return answer

    # ---- a live run's points: its own machine only ----

    def _tell_position(self, position: Any) -> None:
        if self.lab.run_id and self._as_run and isinstance(position, int) and not isinstance(position, bool):
            metric.emit("queue_position", position)

    def _tell_held(self, held: bool) -> None:
        """``held`` 1 and ``held_since`` (the session's start, in seconds since the epoch) when the run's session
        starts, ``held`` 0 when it ends. Only the run's own machine: a copy a run's script holds is not the run."""
        if not (self.lab.run_id and self._as_run):
            return
        if held:
            since = self._held_since or datetime.now(timezone.utc)
            metric.emit("held", 1)
            metric.emit("held_since", round(since.timestamp(), 3))
        else:
            metric.emit("held", 0)

    # ---- the run's session, ended while it runs ----

    def _lost(self, code: str, message: str = "") -> RunSessionEnded | None:
        """A call on this machine was refused with ``code``. Inside a live run, while the block holds the run's
        machine, a refusal that says the run does not hold it (``_NOT_HOLDING``) is looked into: when the machine's
        session is no longer the run's, the run stops. Its manifest says why (``error``), ``held`` 0 and
        ``session_ended`` are told, the reason is said on stderr, and the ``RunSessionEnded`` to raise is answered;
        None when the run still holds the machine or this is not the run's machine. The block's end then ends
        nothing: the session is not the run's to end."""
        if not (self.lab.run_id and self._as_run and self.held) or code not in _NOT_HOLDING:
            return None
        lab = self.lab
        with lab._ending:
            if lab._ended is not None:
                return RunSessionEnded(lab._ended.message, lab._ended.detail)
            if not self.held:
                return None
            idle = time.monotonic() - lab._active_at
            try:
                now = lab._request("GET", f"/v1/machines/{_seg(self.id)}", record=False).data
            except LabError:
                now = None  # the lab said the run does not hold it: that stands
            session = now.get("session") if isinstance(now, dict) else None
            mine = (self.session or {}).get("session_id")
            if isinstance(session, dict) and mine and session.get("session_id") == mine:
                return None  # still the run's: the refusal was about something else
            reason = self._ended_reason(code, idle, session if isinstance(session, dict) else None)
            detail = {"machine": self.public_id or self.id, "session": mine, "refused": {"code": code,
                      "message": message}, "held_since": _utc(self._held_since) if self._held_since else None}
            self.held = False
            lab._ended = self._ended = RunSessionEnded(reason, detail)
            if self._entry is not None and lab.run_folder is not None:
                lab.run_folder.session_ended(self._entry, reason)
            lab._tell_calls()
            self._tell_held(False)
            metric.emit("session_ended", round(time.time(), 3))
            _say(reason)
            return RunSessionEnded(reason, detail)

    def _ended_reason(self, code: str, idle_s: float, holder: dict[str, Any] | None) -> str:
        """Why the run's session ended, as far as the run can tell: the lab ended it at the session's longest time
        or after its idle limit, else someone other than the run ended it."""
        name = self.public_id or self.id
        since = f", held since {_utc(self._held_since)}" if self._held_since else ""
        held_for = time.monotonic() - self._held_at
        if self._max_s and held_for >= self._max_s - _SLACK_S:
            cause = f"reached its longest time ({self._max_s / 60:g} minutes) and the lab ended it"
        elif self._idle_s and idle_s >= self._idle_s - _SLACK_S:
            cause = f"was ended by the lab after {self._idle_s:g} s without a call (its idle limit)"
        else:
            cause = "was ended by someone other than this run (from the bench page, or by the lab's admin)"
        now = ""
        if holder is not None:
            who = holder.get("client_name") or "someone outside your organization"
            started = _parse_time(holder.get("started_at"))
            now = f"; {who} holds {name} now" + (f" (since {_utc(started)})" if started else "")
        return (f"The run's session on {name}{since}, {cause}; the lab refused the run's next call at "
                f"{_utc(datetime.now(timezone.utc))} ({code}){now}. The run stops.")

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

    def moku(self, device_id: str) -> dict[str, Any]:
        """Keyword arguments for the official ``moku`` package on the Moku ``device_id`` of this machine, through the
        lab: any of its instruments, ``MultiInstrument`` and ``Moku`` take them, as at the device::

            from moku.instruments import SpectrumAnalyzer
            with lab.machine(sys.argv[1]) as m:
                with SpectrumAnalyzer(**m.moku("moku")) as sa:
                    sa.set_span(frequency1=0, frequency2=10e6)
                    data = sa.get_data()

        ``ip``: ``127.0.0.1:<port>``, a local stand-in for the Moku (``qupai_lab.moku_forward``) that passes every call
        of the package on to ``<lab>/v1/moku/<machine>/<device_id>/api/…``, where InstruCore answers the Moku's own
        HTTP API behind its checks, with this Lab's key and script header; ``<machine>`` is this machine's public id,
        as ``lab.machines()`` lists it: the live machine, or the simulated copy the block holds. Every version of the
        package works through it (4.3's requests session and 4.4's fast transport alike), and it keeps the writes the
        lab lists in each answer, so a rehearsal's ``changes.json`` has them (InstruCore 0.26.2 on). One stand-in per
        device, until the block ends. ``manage_bitstreams=False``: InstruCore uploads what the Moku lacks on the lab
        PC (moku 4.3); from 4.4 the package reads ``MOKU_API_DISABLE_BITSTREAM_MANAGEMENT`` instead, which this sets in the
        script's environment. ``session_trust_env=False`` and ``session_proxies={}``: the package's ``requests`` session reaches
        127.0.0.1 directly, whatever proxy the environment names. The run's manifest notes the device
        (``official_sdk``). ``moku`` is not a dependency of qupai-lab (``qupai-lab[moku]`` installs it)."""
        if self.kind is None:
            raise LabError("NOT_HELD", f"use the machine in a block first: with lab.machine({self.target!r}) as m")
        target = moku_target(self.lab.url, self.public_id or self.id, device_id)
        version = _installed("moku")
        forwarder = self._forwarders.get(device_id)
        if forwarder is None:
            forwarder = Forwarder(target, self.lab._official_headers("moku", version),
                                  self.lab._official_answer(self, device_id), opener=self.lab._open,
                                  refuse=lambda: self.lab._ended.message if self.lab._ended is not None else None)
            self._forwarders[device_id] = forwarder
        self.lab._official("moku", version, self, device_id)
        # moku 4.4 manages bitstreams unless this is set (its manage_bitstreams argument is gone); the lab uploads
        # what the Moku lacks at the lab PC, and the sandbox has no instrument files to manage.
        os.environ["MOKU_API_DISABLE_BITSTREAM_MANAGEMENT"] = "1"
        return {
            "ip": forwarder.address,
            "manage_bitstreams": False,
            # the package sets each session_<name> on its requests session: straight to the local stand-in
            "session_trust_env": False,
            "session_proxies": {},
        }

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
        lab = self.machine.lab
        lab.check_running()
        counted = False
        try:
            return lab.request("POST", path, args, note=note, unwrap=unwrap)
        except LabError as e:
            lab._called()  # a refused call counts too, and is told with the run's end
            counted = True
            ended = self.machine._lost(e.code, e.message)
            if ended is not None:
                raise ended from e
            raise
        finally:
            if not counted:
                lab._called()

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
