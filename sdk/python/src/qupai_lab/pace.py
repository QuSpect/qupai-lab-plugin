"""Waiting before asking the platform again: its ``429 Too Many Requests``, and a ``503`` that names a
``Retry-After``, bounded, for the calls that may be sent again.

QuPai's gateway answers ``429`` (``urn:qupai:problem:rate``) before it passes a call on to the lab: too many calls
from one address in the minute (``Retry-After: 60``, until the minute ends: a lab workstation's seats share one
address, MCP and REST together), or too many of the member's calls to the lab (``Retry-After`` the seconds left).
Such a call never reached the lab, so it is sent again then, a write included. A call that may be sent again
whatever became of the first (``repeatable``: a read, the machine list, a session's start, renewal and end,
withdrawing a queue request) is sent again after any ``429``, and after a ``503`` that names a ``Retry-After``.
A write is not, unless the refusal is the gateway's own ``429``: InstruCore answers no ``429`` itself, and any other
answer may have come after the write reached the instrument. Every other refusal is raised at once.

Each wait is the ``Retry-After`` the answer names (seconds or an HTTP date; at least ``MIN_S``), else 2 s doubling
with each try; never more than ``WAIT_MAX_S``. One call waits ``TOTAL_MAX_S`` in all at most, and the refusal is then
raised. Standard library only, as the rest of the Lab SDK.
"""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone
from typing import Any

WAIT_MAX_S = 60.0  # one wait at most, whatever Retry-After says
TOTAL_MAX_S = 300.0  # all the waits of one call at most
DEFAULT_S = 2.0  # the first wait when the answer names no Retry-After; it doubles with each try
MIN_S = 1.0  # never sooner (a Retry-After of 0 or a date already past)


def retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (seconds, or an HTTP date); None when there is none or it does not
    parse."""
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def from_gateway(detail: dict[str, Any] | None) -> bool:
    """The refusal is the QuPai gateway's own problem document (``errors.from_response`` marks it ``source``
    ``gateway``): the gateway refused the call itself, at its door."""
    return isinstance(detail, dict) and detail.get("source") == "gateway"


def next_wait(status: int, header: str | None, *, repeatable: bool, not_forwarded: bool, tries: int,
              waited: float) -> float | None:
    """How long to wait before sending the call again after an answer of ``status`` with ``Retry-After``
    ``header``, or None to raise the refusal now. ``repeatable``: the call may be sent again whatever became of the
    first. ``not_forwarded``: the refusal says the call never reached the lab (the gateway's own). ``tries``: the
    waits so far for this call, ``waited`` their seconds."""
    named = retry_after(header)
    if status == 429:
        if not (repeatable or not_forwarded):
            return None
    elif status == 503 and named is not None:
        if not repeatable:
            return None
    else:
        return None
    wait = named if named is not None else DEFAULT_S * 2 ** min(tries, 16)
    wait = min(max(wait, MIN_S), WAIT_MAX_S)
    if waited + wait > TOTAL_MAX_S + 1e-6:
        return None
    return wait


def span(seconds: float) -> str:
    """A time as a sentence says it: ``45 s``, ``4 min 10 s``, ``1 h 5 min``."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min" + (f" {s % 60} s" if s % 60 else "")
    return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 // 60 else "")


def notice(status: int, message: str, wait: float, what: str) -> str:
    """The one line a call says when it starts waiting: who refused, how long it waits, and what it asks again."""
    who = "rate-limited by the platform" if status == 429 else "the platform is unavailable for now (503)"
    said = f": {message[:200]}" if message else ""
    return (f"{who}; waiting {span(wait)}, then asking again ({what}{said}). One call waits "
            f"{span(TOTAL_MAX_S)} at most")
