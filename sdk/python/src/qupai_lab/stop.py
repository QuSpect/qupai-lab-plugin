"""SIGTERM and SIGINT while a machine is held: the script stops by raising, so the ``with`` block releases it.

While any machine is held (``hold`` … ``unhold``) one handler serves both signals:

- the first signal raises in the main thread: ``SystemExit(143)`` for SIGTERM, ``KeyboardInterrupt`` for SIGINT,
  and the blocks' ends release what they hold;
- while a session starts or ends (``shielded``) the signal waits until that call is done, so a session is never
  left half started or half ended; but while a session's start waits for the platform to let it through
  (``unshielded``), nothing is started, and the signal stops the script at once;
- a repeat while the script stops is ignored (a runner forwards the signal its process group also got).

When nothing is held any more the handlers the script had are back. A signal set to be ignored stays ignored, and
outside the main thread nothing is installed (Python runs handlers only there).

``interrupt`` stops the script the same way from another thread (a Moku's local stand-in that learns the run's
session is gone): the exception it is given is raised in the main thread, as a signal's would be.
"""

from __future__ import annotations

import _thread
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

SIGNALS = tuple(s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)) if s is not None)

_depth = 0  # machines held now
_previous: dict[int, Any] = {}
_shield = 0
_deferred: int | None = None
_stopping: int | None = None
_pending: BaseException | None = None  # ``interrupt``'s exception, raised by the handler in the main thread
_pending_lock = threading.Lock()  # ``interrupt`` (another thread) against ``unhold`` (the main thread)


def name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def stopping() -> int | None:
    """The signal that is stopping the script, if one is."""
    return _stopping



def _stop(signum: int) -> BaseException:
    global _stopping
    _stopping = signum
    print(f"qupai-lab: {name(signum)}: releasing the machine, then stopping", file=sys.stderr, flush=True)
    if signum == getattr(signal, "SIGINT", None):
        return KeyboardInterrupt()
    return SystemExit(128 + signum)


def _handler(signum: int, _frame: FrameType | None) -> None:
    global _deferred
    if _shield:
        if _pending is None:
            _deferred = _deferred or signum
        return
    exc = _take()
    if exc is not None:
        raise exc
    if _stopping is not None:
        return
    raise _stop(signum)


def _take() -> BaseException | None:
    global _pending
    with _pending_lock:
        exc, _pending = _pending, None
    return exc


def interrupt(exc: BaseException) -> bool:
    """Raises ``exc`` in the main thread, from another thread, as a signal stops the script: at once, or once a
    session that is starting or ending is done. Only while a machine is held (the handler is installed); False when
    it could not be delivered (nothing held, or the script ignores both signals). SIGTERM's handler carries it when
    it is installed: SIGTERM's default is not a Python handler, so a delivery that comes after the block ended does
    nothing."""
    global _pending
    with _pending_lock:
        signum = next((s for s in SIGNALS if s in _previous), None)
        if _depth == 0 or signum is None:
            return False
        _pending = exc
        _thread.interrupt_main(signum)
    return True


def hold() -> bool:
    """A machine is held from now on: installs the handler when it is the first. False when nothing could be
    installed (not the main thread); ``unhold`` is then not needed."""
    global _depth
    if threading.current_thread() is not threading.main_thread():
        return False
    if _depth == 0:
        _previous.clear()
        for signum in SIGNALS:
            current = signal.getsignal(signum)
            if current is signal.SIG_IGN:
                continue
            _previous[signum] = current
            signal.signal(signum, _handler)
    _depth += 1
    return True


def unhold() -> None:
    """A machine held with ``hold`` is released: the last one puts the script's own handlers back."""
    global _depth, _deferred, _stopping, _pending
    _depth = max(0, _depth - 1)
    if _depth:
        return
    with _pending_lock:
        for signum, previous in _previous.items():
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
        _previous.clear()
        _pending = None
    _deferred = None
    _stopping = None


@contextmanager
def shielded() -> Iterator[None]:
    """A session starting or ending: a signal now takes effect when the block is done."""
    global _shield, _deferred
    _shield += 1
    try:
        yield
    finally:
        _shield -= 1
        exc = _take() if _shield == 0 else None
        if exc is not None:
            _deferred = None
            raise exc
        if _shield == 0 and _deferred is not None and _stopping is None:
            signum, _deferred = _deferred, None
            raise _stop(signum)


@contextmanager
def unshielded() -> Iterator[None]:
    """Inside ``shielded``, a wait while nothing is starting or ending (a session's start waiting for the platform
    to let it through, after a 429): a signal now stops the script at once, one that came before the wait too."""
    global _shield, _deferred
    saved = _shield
    _shield = 0
    try:
        if _deferred is not None and _stopping is None:
            signum, _deferred = _deferred, None
            raise _stop(signum)
        yield
    finally:
        _shield = saved
