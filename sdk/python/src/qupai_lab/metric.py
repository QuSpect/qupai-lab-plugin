"""``QUPAI_METRIC`` lines: points the QuPai Runtime reads from a background job's output (its log).

The Runtime takes output lines that start with ``QUPAI_METRIC `` and a JSON object; ``{"key": k, "value": v}``
is one point of ``k`` (a name of letters, digits and ``_./-``, a finite number), numbered by the Runtime when it
has no ``step``. A lab run's job shows them in the tray (``queue_position`` while it waits).
"""

from __future__ import annotations

import json
import sys

MARKER = "QUPAI_METRIC "


def line(key: str, value: float) -> str:
    return MARKER + json.dumps({"key": key, "value": value}) + "\n"


def emit(key: str, value: float) -> None:
    """Prints one point on stdout as a whole line, at once."""
    try:
        sys.stdout.write(line(key, value))
        sys.stdout.flush()
    except (OSError, ValueError):  # stdout closed: a point is not worth stopping the script
        pass
