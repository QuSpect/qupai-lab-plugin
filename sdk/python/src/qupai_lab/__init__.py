"""The QuPai Lab SDK: work a lab's instruments from Python through InstruCore's REST API (LS4).

    from qupai_lab import Lab

    lab = Lab.from_env(record="calls.jsonl")
    with lab.machine("signal-bench-sim") as m:
        gen = m.role("dg822_pro_usb")
        gen.write("set_waveform", channel=1, shape="sine", frequency_hz=1000, amplitude_v=1.0)
"""

__version__ = "0.6.1"

from qupai_lab.errors import LabError, RunSessionEnded  # noqa: E402  (after __version__, which lab.py imports)
from qupai_lab.changes import declare_changes  # noqa: E402
from qupai_lab.lab import Instrument, Lab, Machine  # noqa: E402

__all__ = ["Instrument", "Lab", "LabError", "Machine", "RunSessionEnded", "__version__", "declare_changes"]
