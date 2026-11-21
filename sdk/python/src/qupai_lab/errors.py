"""Refusals and failures, from InstruCore's error shape or the QuPai gateway's problem documents."""

from __future__ import annotations

import json
from typing import Any

GATEWAY_PROBLEM = "urn:qupai:problem:"


class LabError(Exception):
    """A call the lab, the gateway or the SDK refused or could not make.

    ``code`` is InstruCore's error code (``LIMIT_EXCEEDED``, ``APPROVAL_REQUIRED``, ``RUN_REQUIRED`` …), the
    gateway's problem code in the same form (``RATE``, ``INVALID_KEY``, ``FORBIDDEN`` …) or one of the SDK's own
    (``UNREACHABLE``, ``NOT_CONFIGURED``, ``UNKNOWN_ROLE`` …). ``message`` is the lab's sentence as it was sent.
    ``status`` is the HTTP status, 0 when no answer came.
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None, status: int = 0) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.status = status


def from_response(status: int, body: bytes, content_type: str = "") -> LabError:
    """The error an answer of ``status`` (400 or more) carries.

    InstruCore answers ``{"error": {code, message, detail}, "req_id"}``; the gateway answers
    ``application/problem+json`` with ``type`` ``urn:qupai:problem:<code>``, ``title`` and a ``detail`` sentence.
    """
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text) if text.strip() else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("code"):
            detail = err.get("detail") if isinstance(err.get("detail"), dict) else {}
            if data.get("req_id") and "req_id" not in detail:
                detail = {**detail, "req_id": data["req_id"]}
            return LabError(str(err["code"]), str(err.get("message") or ""), detail, status)
        if "type" in data or "problem+json" in content_type:
            kind = str(data.get("type") or "")
            name = kind[len(GATEWAY_PROBLEM) :] if kind.startswith(GATEWAY_PROBLEM) else ""
            code = name.upper().replace("-", "_") if name else f"HTTP_{status}"
            title = str(data.get("title") or "")
            said = data.get("detail")
            message = str(said) if isinstance(said, str) and said else title or text[:300]
            return LabError(code, message, {"type": kind, "title": title, "source": "gateway"}, status)
        if isinstance(err, str) and err:
            return LabError(f"HTTP_{status}", err, {}, status)
    return LabError(f"HTTP_{status}", text[:300] or f"HTTP {status}", {}, status)
