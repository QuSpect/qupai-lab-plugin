"""A local stand-in for a lab's Moku, for the manufacturer's own ``moku`` package (``Machine.moku``).

The package talks to ``127.0.0.1:<port>`` as to a Moku. Each request is passed on to the lab's copy of that Moku's
HTTP API, ``<lab>/v1/moku/<machine>/<device>/api/…`` (InstruCore 0.26.0 on), with this Lab's key, script header and
user agent; the answer comes back as the lab gave it. So every version of the package works the same way, whatever
it does with the address it is given or with its headers: 4.3 builds ``http://{ip}/api/…`` on a ``requests``
session, while 4.4's fast transport keeps only the host and port of ``ip`` and sends only its ``Moku-Client-Key``.

Each JSON answer is also handed to ``on_answer`` (the Lab keeps the writes the lab lists in it for ``changes``).
Once ``refuse`` answers a sentence (a live run whose session ended), nothing more is passed on: each call is refused
here with it (``SESSION_ENDED``) and told to ``refused`` (the run's manifest lists what did not reach the Moku),
and handing the Moku back succeeds, since there is nothing to hand back.
Standard library only, as the rest of the Lab SDK.
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# How long one call may take at the lab: longer than any the package waits for (a deploy that first uploads
# instrument files takes up to 420 s at InstruCore). The package's own timeouts still apply on its side.
UPSTREAM_TIMEOUT_S = 600.0
# Request headers of the package that are passed on; the Lab's own (key, script, user agent) are added.
PASSED_ON = ("Content-Type", "Moku-Client-Key", "Accept")
# Answer headers passed back to the package.
PASSED_BACK = ("Content-Type", "Moku-Client-Key", "Content-Disposition")
MAX_BODY = 160 * 1024 * 1024  # a settings file or an upload; the lab has its own, smaller limits


def _envelope(code: str, message: str) -> bytes:
    """A refusal in the Moku's own envelope, which the package turns into an exception with the message."""
    return json.dumps({"success": False, "data": None, "code": code, "messages": [message]}).encode()


class Forwarder:
    """One device's stand-in: a small threaded HTTP server on 127.0.0.1 that passes every ``/api/…`` request on to
    ``target`` (``<lab>/v1/moku/<machine>/<device>``)."""

    def __init__(self, target: str, headers: dict[str, str], on_answer: Callable[[int, str, bytes], None],
                 opener: Callable[..., Any] | None = None, refuse: Callable[[], str | None] | None = None,
                 refused: Callable[[str, str], None] | None = None) -> None:
        self.target = target.rstrip("/")
        self.headers = dict(headers)
        self.on_answer = on_answer
        self.refuse = refuse or (lambda: None)
        self.refused = refused or (lambda method, path: None)  # told each call refused here (method, path)
        # How a call goes to the lab: the Lab's own way (straight, or through a lab workstation's door).
        self._open = opener or urllib.request.urlopen
        forwarder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive, as the package's fast transport expects

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - quiet: the lab keeps the record
                return

            def do_GET(self) -> None:
                forwarder._serve(self, "GET")

            def do_POST(self) -> None:
                forwarder._serve(self, "POST")

            def do_PUT(self) -> None:
                forwarder._serve(self, "PUT")

            def do_DELETE(self) -> None:
                forwarder._serve(self, "DELETE")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="qupai-lab-moku", daemon=True)
        self._thread.start()

    @property
    def address(self) -> str:
        """The ``ip`` the package gets: ``127.0.0.1:<port>``."""
        return f"127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── one request ──────────────────────────────────────────────────────────────────────────────

    def _serve(self, h: http.server.BaseHTTPRequestHandler, method: str) -> None:
        try:
            body = self._body(h)
        except ValueError as e:
            self._reply(h, 200, "application/json", _envelope("INVALID_REQUEST", str(e)), {})
            return
        if not h.path.startswith("/api/"):
            self._reply(h, 404, "application/json", _envelope("NOT_FOUND", f"{h.path} is not a Moku API path"), {})
            return
        stopped = self.refuse()
        if stopped is not None:
            if h.path.rstrip("/") == "/api/moku/relinquish_ownership":
                done = json.dumps({"success": True, "data": "Ownership relinquished", "code": None, "messages": []})
                self._reply(h, 200, "application/json", done.encode(), {})
            else:
                try:
                    self.refused(method, h.path)
                except Exception:  # noqa: BLE001 - noting a refusal never breaks the answer
                    pass
                self._reply(h, 200, "application/json", _envelope("SESSION_ENDED", stopped), {})
            return
        headers = {k: h.headers[k] for k in PASSED_ON if h.headers.get(k)}
        headers.update(self.headers)
        req = urllib.request.Request(self.target + h.path, data=body if body else None, method=method, headers=headers)
        try:
            with self._open(req, timeout=UPSTREAM_TIMEOUT_S) as r:
                status, answer, back = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:  # the lab's answer all the same (a 404 of an older InstruCore, a 401)
            status, answer, back = e.code, e.read(), e.headers
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            message = f"the lab at {self.target} did not answer ({reason}); nothing was sent to the Moku"
            self._reply(h, 200, "application/json", _envelope("LAB_UNREACHABLE", message), {})
            return
        content_type = back.get("Content-Type", "")
        if status == 200 and content_type.startswith("application/json"):
            try:
                self.on_answer(status, content_type, answer)
            except Exception:  # noqa: BLE001 - recording a write never breaks the call
                pass
        self._reply(h, status, content_type, answer, {k: back[k] for k in PASSED_BACK[1:] if back.get(k)})

    @staticmethod
    def _body(h: http.server.BaseHTTPRequestHandler) -> bytes:
        """The request's body: by Content-Length, or chunked (a file the package streams)."""
        if h.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int(h.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while h.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(out)
                out += h.rfile.read(size)
                h.rfile.readline()
                if len(out) > MAX_BODY:
                    raise ValueError(f"the request body is larger than {MAX_BODY} bytes")
        length = int(h.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError(f"the request body is larger than {MAX_BODY} bytes")
        return h.rfile.read(length) if length else b""

    @staticmethod
    def _reply(h: http.server.BaseHTTPRequestHandler, status: int, content_type: str, body: bytes,
               headers: dict[str, str]) -> None:
        h.send_response(status)
        if content_type:
            h.send_header("Content-Type", content_type)
        for k, v in headers.items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)
