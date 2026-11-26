"""A lab worked directly, outside QuPai, with the person's own ``qupai`` sign-in (``Lab.from_qupai``, DL8).

The sign-in is what ``qupai login`` keeps: the file ``QUPAI_CREDENTIALS`` names, else ``qupai/credentials.json`` in
the user's configuration directory (macOS ``~/Library/Application Support``, Linux ``$XDG_CONFIG_HOME`` or
``~/.config``, Windows ``%AppData%``). Its ``server`` is the platform and its ``api_key`` the person's own key, which
is sent only as ``Authorization`` and never printed.

The platform says which labs the person may work directly from where they are (``GET /v1/me/lab-access``: QuPai
staff, a grant to their account, or a grant to the workstation they sit at) and each lab's gateway listing. The lab
is then ``<gateway>/rest/<listing>``: InstruCore's REST API behind QuPai's gateway, which checks the grant at every
call. The gateway is the platform's own address unless ``QUPAI_GATEWAY`` names another, as for ``qupai mcp``.

On a lab workstation every call goes through the workstation's door, a local Unix socket
(``QUPAI_WORKSTATION_LAB_SOCKET``, else ``/Library/QuPai/workstation/lab.sock`` when it exists). The door adds the
workstation's own credential, which no seat can read, so a grant to the workstation counts there and nowhere else.
Standard library only, as the rest of the Lab SDK.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from qupai_lab import __version__
from qupai_lab.errors import LabError, from_response

CREDENTIALS_ENV = "QUPAI_CREDENTIALS"
GATEWAY_ENV = "QUPAI_GATEWAY"
SOCKET_ENV = "QUPAI_WORKSTATION_LAB_SOCKET"
# Where the workstation's door listens (``qupai workstation door``); the door serves only the seats of the workstation.
WORKSTATION_SOCKET = Path("/Library/QuPai/workstation/lab.sock")
LAB_ACCESS = "/v1/me/lab-access"
TIMEOUT_S = 30.0
ASK_HINT = 'for any other lab, ask its agent: qupai ask -lab <lab> "…" (or the ask tool of qupai mcp)'

Opener = Callable[..., Any]  # urllib.request.urlopen, or an opener's open: (request, timeout=…) → response


def config_dir() -> Path | None:
    """The user's configuration directory, as the qupai command finds it (Go's ``os.UserConfigDir``); None when
    there is none, as under cron or a service."""
    if sys.platform == "win32":
        app_data = os.environ.get("APPDATA", "")
        return Path(app_data) if app_data and os.path.isabs(app_data) else None
    if sys.platform == "darwin":
        home = os.environ.get("HOME", "")
        return Path(home) / "Library" / "Application Support" if home else None
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        return Path(xdg) if os.path.isabs(xdg) else None
    home = os.environ.get("HOME", "")
    return Path(home) / ".config" if home else None


def credentials_path() -> Path | None:
    """The file ``qupai login`` writes: ``QUPAI_CREDENTIALS``, else ``qupai/credentials.json`` in the user's
    configuration directory; None when there is neither."""
    given = os.environ.get(CREDENTIALS_ENV, "").strip()
    if given:
        return Path(given)
    folder = config_dir()
    return folder / "qupai" / "credentials.json" if folder is not None else None


def signin() -> tuple[str, str, Path]:
    """The person's ``qupai`` sign-in: the platform's address, their API key and the file both came from."""
    path = credentials_path()
    if path is None:
        raise LabError(
            "NOT_SIGNED_IN",
            f"no qupai sign-in: there is no configuration directory here (HOME is unset); set {CREDENTIALS_ENV} to "
            "the file qupai login wrote",
            {"looked": None},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LabError(
            "NOT_SIGNED_IN",
            f"no qupai sign-in on this machine ({path} does not exist): run qupai login, then try again",
            {"looked": str(path)},
        ) from None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise LabError("NOT_SIGNED_IN", f"the qupai sign-in at {path} could not be read: {e}",
                       {"looked": str(path)}) from None
    if not isinstance(data, dict):
        data = {}
    server = str(data.get("server") or "").strip().rstrip("/")
    key = str(data.get("api_key") or "").strip()
    if not key:
        held = "a device sign-in, which has no key" if data.get("access_token") or data.get("refresh_token") else "no key"
        raise LabError(
            "NOT_SIGNED_IN",
            f"the qupai sign-in at {path} holds {held}: run qupai login (the browser sign-in gives this machine a "
            "personal API key), then try again",
            {"looked": str(path)},
        )
    if not server:
        raise LabError("NOT_SIGNED_IN", f"the qupai sign-in at {path} names no platform (server): run qupai login",
                       {"looked": str(path)})
    return server, key, path


def workstation_socket() -> str | None:
    """The workstation's door: ``QUPAI_WORKSTATION_LAB_SOCKET``, else ``/Library/QuPai/workstation/lab.sock`` when
    that socket exists; None on any other machine."""
    given = os.environ.get(SOCKET_ENV, "").strip()
    if given:
        return given
    try:
        return str(WORKSTATION_SOCKET) if WORKSTATION_SOCKET.is_socket() else None
    except OSError:
        return None


# ── HTTP over the workstation's Unix socket ─────────────────────────────────────────────────────


class UnixSocketConnection(http.client.HTTPConnection):
    """An HTTP connection to a Unix socket: the request's address names the platform (its ``Host``), the socket
    carries it. Plain HTTP on the socket, also for an https address: the door makes the TLS connection."""

    def __init__(self, path: str, host: str, timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT, **_: Any) -> None:
        super().__init__(host, timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise OSError(f"this system has no Unix sockets for {self.socket_path}")
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _UnixSocketHandler(urllib.request.AbstractHTTPHandler):
    """urllib's handler for http and https addresses that all go to one Unix socket."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.socket_path = path

    def _connection(self, host: str, **kw: Any) -> UnixSocketConnection:
        return UnixSocketConnection(self.socket_path, host, **kw)

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._connection, req)

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._connection, req)

    http_request = urllib.request.AbstractHTTPHandler.do_request_
    https_request = urllib.request.AbstractHTTPHandler.do_request_


def socket_opener(path: str) -> Opener:
    """``urlopen`` through the Unix socket at ``path``: no proxy (the environment's would take the call elsewhere)
    and no redirect (an answer is the door's or the platform's, as it came); an answer of 400 or more raises
    ``HTTPError`` as ``urlopen``'s does."""
    opener = urllib.request.OpenerDirector()
    for handler in (_UnixSocketHandler(path), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener.open


# ── which labs the person may work directly ─────────────────────────────────────────────────────


@dataclass
class Route:
    """How ``Lab.from_qupai`` reaches a lab: its REST address, the person's key, the workstation's socket (or None)
    and the platform's entry for the lab (``lab``, ``name``, ``listing``, ``role``, ``via``)."""

    url: str
    key: str
    socket: str | None
    entry: dict[str, Any]


def _name(value: Any) -> str:
    """A lab's name as the platform writes it: a string, or one per language."""
    if isinstance(value, dict):
        return str(value.get("en") or next((v for v in value.values() if v), "") or "")
    return str(value or "")


def _where(server: str, unix_socket: str | None) -> str:
    return f"{server}, through the workstation's door at {unix_socket}" if unix_socket else server


def lab_access(server: str, key: str, unix_socket: str | None, opener: Opener, path: Path | None = None) -> dict[str, Any]:
    """``GET /v1/me/lab-access``: ``{labs: [{lab, name, listing, role, via}], ask}`` for the person, from where
    they are."""
    req = urllib.request.Request(server + LAB_ACCESS, method="GET", headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": f"qupai-lab/{__version__}"})
    where = _where(server, unix_socket)
    try:
        with opener(req, timeout=TIMEOUT_S) as resp:
            raw, status = resp.read(), resp.status
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, status = (e.read() if e.fp is not None else b""), e.code
        content_type = e.headers.get("Content-Type", "") if e.headers else ""
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise LabError("UNREACHABLE", f"the platform at {where} is unreachable: {reason}",
                       {"server": server, "socket": unix_socket}) from e
    if status == 404:
        raise LabError("NOT_OFFERED", f"the platform at {where} does not say which labs you may work directly "
                       f"(GET {LAB_ACCESS} answered 404: a platform from before direct lab access); {ASK_HINT}",
                       {"server": server, "socket": unix_socket}, status)
    if status >= 400:
        err = from_response(status, raw, content_type)
        if status in (401, 403):
            err = LabError(err.code, f"{err.message} (the qupai sign-in at {path or 'the qupai credentials'} was "
                           "refused: run qupai login to sign in again)", err.detail, status)
        raise err
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("labs"), list):
        raise LabError("HTTP_" + str(status), f"the platform at {where} answered GET {LAB_ACCESS} with no list "
                       "of labs", {"server": server}, status)
    return data


def _listed(labs: list[Any]) -> str:
    out = []
    for e in labs:
        if not isinstance(e, dict) or not e.get("lab"):
            continue
        via = {"workstation": "through this workstation", "account": "granted to your account",
               "staff": "as QuPai staff"}.get(str(e.get("via") or ""), "")
        name = _name(e.get("name"))
        out.append(f"{e['lab']}{f' ({name})' if name and name != e['lab'] else ''}: {e.get('role') or 'view'}"
                   f"{f', {via}' if via else ''}")
    return "; ".join(out)


def route(lab: str, *, opener: Opener | None = None) -> Route:
    """How to reach ``lab`` (its id, as ``qupai lab list`` names it, or its listing) directly: refuses
    (``NO_DIRECT_ACCESS``) a lab the person may not work directly from here, naming the labs they may."""
    lab = (lab or "").strip()
    if not lab:
        raise LabError("NOT_CONFIGURED", 'Lab.from_qupai(lab) needs the lab\'s id, such as Lab.from_qupai("magnetic")')
    server, key, path = signin()
    unix_socket = workstation_socket()
    open_ = opener or (socket_opener(unix_socket) if unix_socket else urllib.request.urlopen)
    access = lab_access(server, key, unix_socket, open_, path)
    labs = access["labs"]
    entry = next((e for e in labs if isinstance(e, dict) and lab in (e.get("lab"), e.get("listing"))), None)
    if entry is None or not entry.get("listing"):
        mine = _listed(labs)
        here = "from this workstation" if unix_socket else "from this machine"
        may = f"you may work these labs directly {here}: {mine}" if mine else (
            f"you may work no lab directly {here} (direct access is a grant QuPai staff make)")
        raise LabError(
            "NO_DIRECT_ACCESS",
            f"{lab!r} is not a lab you may work directly {here}; {may}. Otherwise, ask is the way: {ASK_HINT}, and "
            "the agent does the lab work in QuPai",
            {"lab": lab, "labs": [e.get("lab") for e in labs if isinstance(e, dict)], "ask": bool(access.get("ask")),
             "socket": unix_socket},
        )
    gateway = os.environ.get(GATEWAY_ENV, "").strip().rstrip("/") or server
    listing = str(entry["listing"])
    return Route(f"{gateway}/rest/{quote(listing, safe='')}", key, unix_socket, dict(entry))
