"""SIGTERM and SIGINT while a machine is held: the script stops by raising, so the ``with`` block releases it.

While any machine is held (``hold`` … ``unhold``) one handler serves both signals:

- the first signal raises in the main thread: ``SystemExit(143)`` for SIGTERM, ``KeyboardInterrupt`` for SIGINT,
  and the blocks' ends release what they hold;
- while a session starts or ends (``shielded``) the signal waits until that call is done, so a session is never
  left half started or half ended;
- a repeat while the script stops is ignored (a runner forwards the signal its process group also got).

When nothing is held any more the handlers the script had are back. A signal set to be ignored stays ignored, and
outside the main thread nothing is installed (Python runs handlers only there).
"""

from __future__ import annotations

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
    if _stopping is not None:
        return
    if _shield:
        _deferred = _deferred or signum
        return
    raise _stop(signum)


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
    global _depth, _deferred, _stopping
    _depth = max(0, _depth - 1)
    if _depth:
        return
    for signum, previous in _previous.items():
        signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
    _previous.clear()
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
        if _shield == 0 and _deferred is not None and _stopping is None:
            signum, _deferred = _deferred, None
            raise _stop(signum)
