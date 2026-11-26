"""The Lab SDK over InstruCore's REST API: machines, sessions on simulated copies and on a run's live machine,
instruments by role."""

from __future__ import annotations

import atexit
import csv
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

from qupai_lab import __version__, config, metric, pace, stop
from qupai_lab import direct as _direct
from qupai_lab import changes as _changes
from qupai_lab.moku_forward import Forwarder
from qupai_lab import runs as _runs
from qupai_lab.errors import LabError, RunSessionEnded, from_response
from qupai_lab.record import Recorder, dumps, ensure_parent, now_iso

TIMEOUT_S = 150.0
POLL_S = 2.0
# A queued request is looked at every POLL_S at first, then less often while its place does not move, up to
# POLL_MAX_S: a run may wait hours, and each look counts against the member's per-minute calls to the lab. An
# offer is kept for minutes (InstruCore's offer_s, 300 s by default), so POLL_MAX_S still starts it in time.
POLL_MAX_S = 15.0
# A block that waits for its machine says so when it starts waiting, then this often: what it waits for (who holds
# the machine, its place in the queue) and since when. The run's manifest keeps it too (``waits``).
SAY_EVERY_S = 30.0
# The calls that may be sent again whatever became of the first (``pace``): an instrument's read and check ops, a
# session's start, renewal and end. GET (the machine list, a machine, a queue request, a reference) and withdrawing
# a queue request (DELETE) are too.
_REPEATABLE_POST = re.compile(r"/v1/instruments/[^/]+/(?:read|check)/[^/]+|/v1/machines/[^/]+/sessions(?:/renew|/end)?")
_REPEATABLE_DELETE = re.compile(r"/v1/session-requests/[^/]+")
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
# After a run's session ended under it, the longest a session the run takes to put its settings back may last (the lab
# gives the smaller of this and its own limits); the SDK ends it at the block's end.
PUT_BACK_MAX_S = 120.0
# Arguments that say which of an instrument's settings a write sets (CH1 and CH4 are two settings, not one).
_WHICH = ("channel", "ch", "slot", "output", "port", "axis", "index", "input", "source")
_LISTED = 8  # settings a sentence names before "and N more"
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


def _repeatable(method: str, path: str) -> bool:
    """A call that may be sent again whatever became of the first (``pace``): a GET, an instrument's read or check
    op, a session's start, renewal or end, withdrawing a queue request. A write is not."""
    path = path.split("?", 1)[0]
    if method in ("GET", "HEAD"):
        return True
    if method == "POST":
        return _REPEATABLE_POST.fullmatch(path) is not None
    if method == "DELETE":
        return _REPEATABLE_DELETE.fullmatch(path) is not None
    return False


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


def _cleaning_up() -> bool:
    """The script is handling an exception right now: in an ``except`` or ``finally`` clause as one goes by, or a
    ``with`` block's exit. That is where a stopping script puts back what it changed."""
    return sys.exc_info()[1] is not None


def _which(args: dict[str, Any]) -> dict[str, Any]:
    """The arguments of a write that say which setting it sets (``_WHICH``)."""
    return {k: args[k] for k in _WHICH if k in args}


def _setting(w: _changes.Write) -> str:
    """A setting as a sentence names it: ``vendor.set_frontend of spectrum (channel 4)``."""
    which = ", ".join(f"{k} {v}" for k, v in _which(w.args).items())
    return f"{w.op} of {w.role}" + (f" ({which})" if which else "")


def _settings(writes: list[_changes.Write]) -> str:
    named = [_setting(w) for w in writes[:_LISTED]]
    more = len(writes) - len(named)
    return ", ".join(named) + (f" and {more} more" if more > 0 else "")


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
        self._clock: Callable[[], float] = time.monotonic  # how long a wait has lasted (tests give their own)
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

        The platform's ``429`` (and a ``503`` that names a ``Retry-After``) waits for ``Retry-After`` (2 s doubling
        without one; 60 s at most each time, 5 minutes in all) and asks again, said once on stderr, for a call that
        may be sent again (a read, the machine list, a session's start, renewal and end). A write is asked again only
        after the gateway's own ``429``, which it answers before the call reaches the lab (``pace``). Each call is
        one line of the record; ``note`` adds fields to it. ``unwrap``: answer an op call's ``result``."""
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
        retry: bool = True,
        stoppable: bool = False,
    ) -> _Answer:
        """``force``: a session's end, which a run that stopped still sends for another machine. ``retry``: wait for
        the platform and ask again (``pace``); False answers its refusal at once (a look that may fail). ``stoppable``:
        a session's start, sent while signals wait (``stop.shielded``): a signal that comes while it waits to ask
        again stops the script there, since nothing was started."""
        if not force:
            self.check_running()
        line = self._line(method, path, body, note)
        if record:
            self._ran()
        tries, waited = 0, 0.0
        while True:
            try:
                answer = self._send(method, path, body)
                self._active_at = time.monotonic()
                break
            except LabError as e:
                headers = getattr(e, "_headers", None)
                wait = self._wait_before_again(e, headers, method, path, tries, waited) if retry else None
                if wait is not None:
                    self._pause(wait, stoppable)
                    tries, waited = tries + 1, waited + wait
                    continue
                if tries:
                    e.detail = {**e.detail, "retries": tries, "waited_s": round(waited, 1)}
                if record:
                    req_id = e.detail.get("req_id") or (headers.get("X-Request-ID") if headers else None)
                    line |= {"ok": False, "status": e.status, "code": e.code, "message": e.message, "req_id": req_id}
                    self.recorder.add(line | _retried(tries, waited))
                raise
        data = answer.data
        req_id = data.get("req_id") if isinstance(data, dict) else None
        if unwrap and isinstance(data, dict) and "result" in data:
            answer.data = data["result"]
        if record:
            self._record_ok(line, answer, req_id, tries, waited)
        return answer

    def _wait_before_again(self, e: LabError, headers: Any, method: str, path: str, tries: int,
                           waited: float) -> float | None:
        """How long to wait before sending the call again after the refusal ``e``, or None to raise it (``pace``).
        The first wait of a call is said on stderr, and so is giving up after waiting; the run's manifest counts
        the waits (``rate_limited``)."""
        header = headers.get("Retry-After") if headers else None
        repeatable = _repeatable(method, path)
        wait = pace.next_wait(e.status, header, repeatable=repeatable,
                              not_forwarded=e.status == 429 and pace.from_gateway(e.detail), tries=tries, waited=waited)
        what = f"{method} {path}"
        if wait is None:
            if tries:
                _say(f"the platform still refuses {what} after {pace.span(waited)} of waiting ({e.status} {e.code}); "
                     "giving up")
            elif e.status in (429, 503) and not repeatable:
                _say(f"{what} was refused ({e.status} {e.code}) and is not sent again: a write that may have reached "
                     "the lab is never sent twice")
            return None
        if tries == 0:
            _say(pace.notice(e.status, e.message, wait, what))
        self._waited_for_platform(wait)
        return wait

    def _waited_for_platform(self, wait: float) -> None:
        """A call waited ``wait`` s for the platform before it was sent again: one of this Lab's, or one of a
        manufacturer's SDK at its local stand-in (``m.moku``). The run's manifest counts it (``rate_limited``)."""
        if self.run_folder is not None:
            self.run_folder.rate_limited(wait)

    def _pause(self, wait: float, stoppable: bool) -> None:
        """Waits ``wait`` s before a call is sent again. ``stoppable`` (a session's start, sent while signals wait):
        a signal during the wait stops the script at once (``stop.unshielded``), since nothing was started."""
        if stoppable:
            with stop.unshielded():
                self._sleep(wait)
        else:
            self._sleep(wait)

    def _record_ok(self, line: dict[str, Any], answer: _Answer, req_id: str | None = None, retries: int = 0,
                   waited: float = 0.0) -> None:
        req_id = req_id or (answer.headers.get("X-Request-ID") if answer.headers else None)
        line |= {"ok": True, "status": answer.status, "code": None, "req_id": req_id}
        self.recorder.add(line | _retried(retries, waited), answer.data, has_result=True)

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


def _retried(tries: int, waited: float) -> dict[str, Any]:
    """A record line's ``retries`` and ``waited_s``, when the call waited for the platform and was sent again."""
    return {"retries": tries, "waited_s": round(waited, 1)} if tries else {}


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
        self._ended_cause: str | None = None  # "longest", "idle" or "other" (``_ended_reason``)
        # After the run's session ended: how many of the Lab's writes came before (what the run set), the session the
        # run took to put its settings back (or why it could not), and each call made to put them back.
        self._writes_before_end = 0
        self._put_back: dict[str, Any] | None = None
        self._put_back_asked = False
        self._session_taken: str | None = None
        self._put_back_why: str | None = None
        self._after: list[dict[str, Any]] = []
        self._put_back_writes: list[_changes.Write] = []
        self._exited = False

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
            self._hold(live, pool or machine)
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
            self._end_put_back()
        finally:
            self._exited = True
            self._unguard()
            for forwarder in self._forwarders.values():
                forwarder.close()
            self._forwarders.clear()
            self.lab.recorder.flush()
        # The run's session ended while the block ran: the block never ends as if it had run (a script that caught
        # what a call raised, or the moku package's own exception, leaves it with the reason), and the reason says
        # what was put back and what may still be set.
        lost = self._ended
        if lost is not None:
            message = self._restored()
            if isinstance(exc, RunSessionEnded):
                exc.message = message
            else:
                raise RunSessionEnded(message, lost.detail) from exc

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

    def _hold(self, live: bool, seen: dict[str, Any] | None = None) -> None:
        """``POST sessions`` with the queue on: 201 is the session; 202 is a queue request, looked at until it is
        offered, then asked again (which starts the session within the offer). Leaving early withdraws it. While it
        waits it says so (``_Waiting``): what for, when it starts waiting, then every ``SAY_EVERY_S``. ``seen``: the
        machine or the pool as the lab listed it just before (who holds it, how many copies are free).

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
        lab = self.lab
        started = lab._clock()
        request = (answer.data or {}).get("request") or {}
        rid = request.get("request_id")
        if not rid:
            raise LabError("SESSION_PENDING", f"queued for {self.target} without a request id", answer.data or {})
        waiting = _Waiting(self, live, answer.data, request, seen)
        position = request.get("position")
        self._tell_position(position)
        still = 0  # looks since the position last moved
        outcome = "failed"
        try:
            while True:
                if self.wait_s is not None and lab._clock() - started >= self.wait_s:
                    outcome = "timeout"
                    raise LabError(
                        "WAIT_TIMEOUT",
                        f"{what} was not offered within {self.wait_s:g} s; the request was withdrawn",
                        {"request_id": rid, "machine": self.target},
                    )
                lab._sleep(min(lab.poll_s * 2**still, max(POLL_MAX_S, lab.poll_s)))
                poll = f"/v1/session-requests/{_seg(rid)}"
                looked = lab._request("GET", poll, record=False)
                status = (looked.data or {}).get("status")
                if status == "pending":
                    still = min(still + 1, 8)
                    if looked.data.get("position") != position:
                        still = 0
                        position = looked.data.get("position")
                        waiting.moved(position)
                        self._tell_position(position)
                    waiting.tick()
                    continue
                lab._record_ok(lab._line("GET", poll, None), looked)  # one line when it moves on
                if status in _GONE:
                    outcome = str(status)
                    raise LabError(
                        f"REQUEST_{str(status).upper()}",
                        f"the queue request for {self.target} was {status} before {what} was offered",
                        {"request_id": rid, "machine": self.target},
                    )
                answer = self._ask(path, body)  # offered: ask again, which starts it
                if answer.status != 202:
                    waiting.done("held")
                    return
                request = (answer.data or {}).get("request") or {}  # the offer ran out or was withdrawn
                rid = request.get("request_id") or rid
        except BaseException as e:
            waiting.done("stopped" if isinstance(e, (KeyboardInterrupt, SystemExit)) else outcome)
            try:
                with stop.shielded():
                    lab.request("DELETE", f"/v1/session-requests/{_seg(rid)}")
            except LabError:
                pass
            raise

    def _ask(self, path: str, body: dict[str, Any]) -> _Answer:
        """One ``POST sessions``; a 201 is taken at once, so a signal cannot fall between the start and the
        hold."""
        with stop.shielded():
            answer = self.lab._request("POST", path, body, stoppable=True)
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
            self._writes_before_end = len(lab._writes)
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
            self._ended_cause = "longest"
            cause = f"reached its longest time ({self._max_s / 60:g} minutes) and the lab ended it"
        elif self._idle_s and idle_s >= self._idle_s - _SLACK_S:
            self._ended_cause = "idle"
            cause = f"was ended by the lab after {self._idle_s:g} s without a call (its idle limit)"
        else:
            self._ended_cause = "other"
            cause = "was ended by someone other than this run (from the bench page, or by the lab's admin)"
        now = ""
        if holder is not None:
            who = holder.get("client_name") or "someone outside your organization"
            started = _parse_time(holder.get("started_at"))
            now = f"; {who} holds {name} now" + (f" (since {_utc(started)})" if started else "")
        return (f"The run's session on {name}{since}, {cause}; the lab refused the run's next call at "
                f"{_utc(datetime.now(timezone.utc))} ({code}){now}. The run stops.")

    # ---- putting back what the run set, after its session ended ----

    def _after_end(self, inst: Instrument, kind: str, op: str, args: dict[str, Any], unwrap: bool) -> Any:
        """A call on the run's machine after its session ended, made while the script stops (``_cleaning_up``: its
        ``finally``, an ``except``, a ``with`` block's exit): the script putting back what the run changed. It goes
        through a session the run takes for that (``_take_back``) when the lab lets it; otherwise it reaches nothing,
        raises ``LabError`` ``NOT_PUT_BACK`` (so the script's own ``except LabError`` sees it and its clean-up goes on)
        and the manifest's ``restore`` lists it."""
        lab = self.lab
        name = self.public_id or self.id
        session = self._take_back()
        if session is None:
            why = self._put_back_why or "the run's session had ended"
            self._note_after(inst, kind, op, args, ok=False, why=why)
            raise LabError("NOT_PUT_BACK", f"{op} of {inst.role} did not reach {name}: the run's session there had "
                           f"ended, and {why}", {"machine": name, "role": inst.role, "op": op, "why": why})
        note = {"machine": name, "role": inst.role, "instrument": inst.id, "kind": kind, "op": op, "put_back": True}
        path = f"/v1/instruments/{_seg(inst.id)}/{kind}/{_seg(op)}"
        try:
            answer = lab._request("POST", path, args, note=note, unwrap=unwrap, force=True)
        except LabError as e:
            self._note_after(inst, kind, op, args, ok=False, why=f"{e.code}: {e.message}")
            raise
        finally:
            lab._called()
        self._note_after(inst, kind, op, args, ok=True)
        return answer.data

    def _take_back(self) -> dict[str, Any] | None:
        """The session the run takes to put its settings back, once: only inside the machine's block, when the session
        did not end at the run's longest time (its approved time is over), the machine is free and nobody waits for it.
        The lab checks the run's key and its approved changes as in the run itself. None, with ``_put_back_why``, when
        it cannot be taken (nor on its own: it is never queued for)."""
        if self._put_back is not None or self._put_back_asked:
            return self._put_back
        self._put_back_asked = True
        lab = self.lab
        name = self.public_id or self.id
        why: str | None = None
        if self._exited:
            why = f"the script's block on {name} had ended"
        elif self._ended_cause == "longest":
            why = "the run's longest time is over"
        else:
            try:
                now = lab._request("GET", f"/v1/machines/{_seg(self.id)}", record=False, force=True).data
            except LabError as e:
                now, why = None, f"the lab did not say whether {name} is free ({e.code})"
            holder = now.get("session") if isinstance(now, dict) else None
            if why is None and isinstance(holder, dict):
                why = f"{holder.get('client_name') or 'someone outside your organization'} holds {name} now"
            elif why is None and isinstance(now, dict) and (now.get("queue") or now.get("offer")):
                why = f"someone is waiting for {name}"  # in its queue, or offered it already
        if why is None:
            body = {"purpose": f"run {lab.run_id}: putting back its settings"[:256], "queue": False,
                    "max_s": PUT_BACK_MAX_S}
            try:
                with stop.shielded():
                    answer = lab._request("POST", f"/v1/machines/{_seg(self.id)}/sessions", body, force=True)
            except LabError as e:
                why = f"the lab gave the run no session to put them back ({e.code}: {e.message})"
            else:
                if answer.status == 201 and isinstance(answer.data, dict):
                    self._put_back = answer.data
                    self._session_taken = str(answer.data.get("session_id") or "") or None
                else:  # queued after all: never wait here
                    why = f"the lab would only queue the run for {name}"
                    rid = ((answer.data or {}).get("request") or {}).get("request_id") if isinstance(
                        answer.data, dict) else None
                    if rid:
                        try:
                            lab._request("DELETE", f"/v1/session-requests/{_seg(rid)}", record=False, force=True)
                        except LabError:
                            pass
        if why is not None:
            self._put_back_why = why
            _say(f"the run's settings on {name} cannot be put back: {why}")
        else:
            _say(f"putting back the run's settings on {name} in session {self._session_taken}")
        self._keep_restore()
        return self._put_back

    def _end_put_back(self) -> None:
        """Ends the session taken to put the run's settings back (the block's end)."""
        session, self._put_back = self._put_back, None
        if session is None:
            return
        try:
            with stop.shielded():
                self.lab._request("POST", f"/v1/machines/{_seg(self.id)}/sessions/end",
                                  {"reason": "qupai-lab: the run's settings are put back"}, force=True)
        except LabError as e:
            if e.code != "NOT_FOUND":
                _say(f"could not end the session {session.get('session_id')} on {self.public_id or self.id}: {e}")

    def _note_after(self, inst: Instrument, kind: str, op: str, args: dict[str, Any], *, ok: bool,
                    why: str | None = None) -> None:
        """One call made after the run's session ended, for the manifest's ``restore``: its writes only."""
        if kind != "write":
            return
        entry: dict[str, Any] = {"role": inst.role, "instrument": inst.id, "op": op, "args": dict(args),
                                 "at": now_iso(), "put_back": ok}
        if why:
            entry["why"] = why
        self._after.append(entry)
        if ok:
            self._put_back_writes.append(_changes.Write(inst.role, op, dict(args), self.public_id or self.target))
        self._keep_restore()

    def moku_refused(self, device: str, method: str, path: str) -> None:
        """A call of the official moku package that the local stand-in refused after the run's session ended: one that
        would have changed something (not a ``get_``, nor handing the Moku back) is listed as not put back."""
        if self._ended is None:
            return
        operation = path.split("?", 1)[0].removeprefix("/api/").strip("/")
        last = operation.rsplit("/", 1)[-1]
        if method == "GET" or last.startswith("get_") or last in ("describe", "relinquish_ownership"):
            return
        self._after.append({"device": device, "call": f"{method} {operation}", "at": now_iso(), "put_back": False,
                            "why": "the moku package's calls after the run's session ended are not passed on"})
        self._keep_restore()

    def _left_set(self) -> list[_changes.Write]:
        """What the run set on this machine while it held it and did not put back: the last write of each setting
        (role, op and the arguments that say which, ``_WHICH``), less the settings written again in a put-back."""
        machine = self.public_id or self.target
        last: dict[str, _changes.Write] = {}
        for w in self.lab._writes[: self._writes_before_end]:
            if w.machine in (machine, self.target, self.id):
                key = json.dumps([w.role, w.op, _which(w.args)], sort_keys=True, default=str)
                last.pop(key, None)
                last[key] = w
        for w in self._put_back_writes:
            last.pop(json.dumps([w.role, w.op, _which(w.args)], sort_keys=True, default=str), None)
        return list(last.values())

    def _keep_restore(self) -> None:
        """The manifest's ``restore``: what happened to the run's settings after its session ended."""
        folder = self.lab.run_folder
        if folder is None or self._ended is None:
            return
        folder.restore({
            "machine": self.public_id or self.id,
            "session": self._session_taken,
            "why_not": self._put_back_why,
            "after_end": list(self._after),
            "left_set": [{"role": w.role, "op": w.op, "args": w.args} for w in self._left_set()],
        })

    def _restored(self) -> str:
        """The block's end after the run's session ended: the reason, with what was put back and what may still be
        set, said on stderr and kept as the manifest's ``error``."""
        lost = self._ended
        assert lost is not None
        name = self.public_id or self.id
        done = [a for a in self._after if a.get("put_back")]
        failed = [a for a in self._after if not a.get("put_back")]
        left = self._left_set()
        said: list[str] = []
        if done:
            sid = self._session_taken
            said.append(f"Its clean-up put back {len(done)} setting{'s' if len(done) != 1 else ''} on {name}"
                        + (f" in session {sid}, which it then ended" if sid else "") + ".")
        if failed:
            why = self._put_back_why or str(failed[0].get("why") or "")
            said.append(f"{len(failed)} change{'s' if len(failed) != 1 else ''} of its clean-up did not reach {name}"
                        + (f" ({why})" if why else "") + ".")
        if left:
            said.append(f"Still set on {name} by this run, as far as it knows: {_settings(left)} (the manifest's "
                        "restore.left_set): check them before the next run.")
        message = " ".join([lost.message, *said])
        if said:
            _say(" ".join(said))
        self._keep_restore()
        folder = self.lab.run_folder
        if folder is not None:
            folder.ended_error(message)
        return message

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
                                  refuse=lambda: self.lab._ended.message if self.lab._ended is not None else None,
                                  refused=lambda method, path, d=device_id: self.moku_refused(d, method, path),
                                  waited=self.lab._waited_for_platform)
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


class _Waiting:
    """A block waiting in its machine's queue (``Machine._hold``), said and kept as it goes. On stderr: one line when
    it starts waiting, saying what it waits for (who holds the machine and since when, or how many copies of a pool
    are free, and its place in the queue, as the lab says them), a line when its place moves, a short one every
    ``SAY_EVERY_S`` while it waits (the holder looked up again), and one when it holds the machine. In the run's
    manifest: one of ``waits``, ``{machine, for, since, position, holder, lab_said, waited_s, until, outcome}``,
    ``outcome`` ``waiting`` while it waits, then ``held``, ``timeout``, ``cancelled``, ``expired``, ``stopped`` or
    ``failed``."""

    def __init__(self, m: Machine, live: bool, data: Any, request: dict[str, Any],
                 seen: dict[str, Any] | None) -> None:
        self.m = m
        self.lab = m.lab
        self.what = "its turn" if live else "a copy"
        self.started = self.said_at = self.lab._clock()
        self.position = request.get("position")
        error = data.get("error") if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        detail = error.get("detail") if isinstance(error.get("detail"), dict) else {}
        self.member_cap = "member_cap" in (detail.get("reason"), request.get("wait_reason"))
        self.pool = isinstance(seen, dict) and "copies" in seen
        self.holder = _holder(seen)
        self.ended = False
        folder = self.lab.run_folder
        self.entry = folder.waiting({
            "machine": m.target, "for": self.what, "since": now_iso(), "position": self.position,
            "holder": self.holder, "lab_said": str(error.get("message") or "") or None, "waited_s": 0,
            "until": None, "outcome": "waiting"}) if folder is not None else None
        why = " (you hold as many simulated sessions as a member may)" if self.member_cap else ""
        held = _holder_text(self.holder)
        _say(f"queued for {m.target} at position {self.position}{why}; waiting for {self.what}"
             + (f": {held}" if held else "") + f". It says how the wait goes every {pace.span(SAY_EVERY_S)}; "
             "SIGINT or SIGTERM leaves the queue")

    def moved(self, position: Any) -> None:
        self.position = position
        self.said_at = self.lab._clock()
        _say(f"waiting for {self.m.target}: position {position}")
        self._keep()

    def tick(self) -> None:
        """After each look at the queue request: every ``SAY_EVERY_S``, a line with the place, the time waited and
        the holder as the lab says it now."""
        now = self.lab._clock()
        if now - self.said_at < SAY_EVERY_S:
            return
        self.said_at = now
        self._look()
        held = _holder_text(self.holder)
        _say(f"still waiting for {self.m.target} ({pace.span(now - self.started)}): position {self.position}"
             + (f"; {held}" if held else ""))
        self._keep()

    def done(self, outcome: str) -> None:
        if self.ended:
            return
        self.ended = True
        waited = self.lab._clock() - self.started
        if outcome == "held":
            _say(f"{self.m.target} is held after waiting {pace.span(waited)} in its queue")
        self._keep(until=now_iso(), outcome=outcome)

    def _look(self) -> None:
        """The holder now: the machine, or the pool in the machine list. A look that fails keeps the last one
        (it is not waited for: the next look is soon)."""
        target = self.m.target
        try:
            if self.pool:
                listing = self.lab._request("GET", "/v1/machines", record=False, retry=False).data
                seen = _find((listing or {}).get("pools") or [], target) if isinstance(listing, dict) else None
            else:
                seen = self.lab._request("GET", f"/v1/machines/{_seg(target)}", record=False, retry=False).data
        except LabError:
            return
        self.holder = _holder(seen if isinstance(seen, dict) else None)

    def _keep(self, **more: Any) -> None:
        folder = self.lab.run_folder
        if folder is None or self.entry is None:
            return
        folder.waited(self.entry, position=self.position, holder=self.holder,
                      waited_s=round(self.lab._clock() - self.started, 1), **more)


def _holder(seen: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a block waits behind, from the machine or the pool as the lab lists it: the session's holder (``name``,
    null when the lab may not say it, ``since``, ``run``, ``ends_in_s``: its longest time left), a pool's ``copies``
    and ``free``, or that the machine is ``offered`` to someone before it."""
    if not isinstance(seen, dict):
        return None
    if "copies" in seen:
        copies = seen.get("copies")
        return {"copies": len(copies) if isinstance(copies, list) else copies, "free": seen.get("free")}
    session = seen.get("session")
    if isinstance(session, dict):
        return {"name": session.get("client_name") or None, "since": session.get("started_at"),
                "run": session.get("run"), "ends_in_s": _seconds(session.get("max_expires_in_s"))}
    if seen.get("offer"):
        return {"offered": True}
    return None


def _holder_text(holder: dict[str, Any] | None) -> str:
    """``held by Ada since 2026-11-26 05:25:19 UTC (run 01a2…); its session ends in 25 min at the latest``."""
    if not holder:
        return ""
    if "copies" in holder:
        copies, free = holder.get("copies"), holder.get("free")
        return f"all {copies} copies are in use" if not free else f"{free} of {copies} copies free"
    if holder.get("offered"):
        return "it is offered to someone before this script in its queue"
    since = _parse_time(holder.get("since"))
    text = f"held by {holder.get('name') or 'someone outside your organization'}"
    text += f" since {_utc(since)}" if since else ""
    text += f" (run {holder['run']})" if holder.get("run") else ""
    ends = holder.get("ends_in_s")
    return text + (f"; its session ends in {pace.span(ends)} at the latest" if ends else "")


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
        m = self.machine
        lab = m.lab
        # A script that stops (an except, a finally, a with's exit) after the run's session ended puts back what it
        # changed: such a call goes through a session taken for it, when the lab lets the run take one (Machine).
        cleaning = _cleaning_up()
        if lab._ended is not None and m._ended is not None and cleaning:
            return m._after_end(self, kind, op, args, unwrap)
        lab.check_running()
        counted = False
        try:
            return lab.request("POST", path, args, note=note, unwrap=unwrap)
        except LabError as e:
            lab._called()  # a refused call counts too, and is told with the run's end
            counted = True
            ended = m._lost(e.code, e.message)
            if ended is not None and cleaning and m._ended is not None:
                return m._after_end(self, kind, op, args, unwrap)
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
