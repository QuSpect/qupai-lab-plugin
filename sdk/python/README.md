# QuPai Lab SDK (`qupai-lab`)

Work a QuPai lab's instruments from Python: the lab's machines, sessions on simulated copies, instruments by
role, and the files an experiment keeps. It calls InstruCore's own REST API (not MCP), so one script runs in a
QuPai lab project's sandbox and at the lab PC. Python 3.10 or later, standard library only.

## Install

Not on PyPI yet. From a checkout of this repository:

```
pip install ./sdk/python
```

or straight from GitHub:

```
pip install "git+https://github.com/QuSpect/qupai-lab-plugin#subdirectory=sdk/python"
```

Then check that this sandbox (or PC) reaches its lab:

```
$ qupai-lab machines
Lab http://172.17.0.1:18399/lab/lab-sim
MACHINE               KIND       STATE        INSTRUMENTS BY ROLE
signal-bench-sim      pool       3 of 4 free
  signal-bench-sim-1  simulated  free         generator=signal_bench_sim_1_generator, scope=signal_bench_sim_1_scope
  …
```

`qupai-lab machines --json` prints the same as JSON; `qupai-lab --version` the version.

## Where the lab is

`Lab.from_env()` reads `QUPAI_LAB_URL` and `QUPAI_LAB_KEY` from the environment, else from the nearest `.env` up
from the working directory, else from `/workspace/project/.env`. In a QuPai lab project the project's `.env`
holds both: the lab's address at the sandbox door and the project key. At a lab PC, give InstruCore's address and
a local key: `Lab("http://127.0.0.1:59220", "ick_…")`.

## A script

```python
import sys
from qupai_lab import Lab

lab = Lab.from_env(record="calls.jsonl")
with lab.machine(sys.argv[1]) as m:            # a pool: holds a free copy, or waits in its queue for one
    gen, scope = m.role("generator"), m.role("scope")
    rows = []
    for f in [100, 300, 1e3, 3e3, 1e4, 3e4, 1e5]:
        gen.write("set_waveform", channel=1, shape="sine", frequency_hz=f, amplitude_v=1.0)
        scope.write("set_timebase", scale_s=0.5 / f, offset_s=0.0)
        trace = scope.read("acquire", channel=1, points=2000)
        rows.append({"frequency_hz": f, **lab.save(f"data/raw/trace_{f:g}.json", trace)})
lab.table("data/response.csv", rows)
lab.changes("changes.json")                    # the writes above, for a live run's card
```

`python run.py signal-bench-sim` runs it on a simulated copy. The roles are the machine's: `qupai-lab machines`
lists them. A script addresses instruments by role, so the same script works on any copy of a pool and on the
machine the pool simulates, when their roles have the same names.

| Call | What it does |
| --- | --- |
| `lab.machines()` | Each live machine and simulated copy: `id` (public id), `internal_id`, `name`, `kind`, `state`, `status`, `pool`, `roles` (role → instrument id) |
| `lab.machine(id, *, purpose="", wait_s=None)` | A machine for a `with` block (below) |
| `m.role(name)` | The instrument in that role |
| `inst.read(op, **args)` | A read op; answers its result. Anyone may read |
| `inst.write(op, **args)` | A write op in the machine's session; the lab checks its limits first |
| `inst.check(op, **args)` | Would this write pass policy, arguments and limits? `{"ok": true, "kind": "write"}` or the refusal, without a session |
| `inst.reference()`, `m.reference()` | The reference document (`mhs-reference/0`): ops, units, limits, notes |
| `m.renew()` | Restarts the held session's time limit, unless someone waits |
| `lab.save(path, value, **meta)` | Writes `value` as JSON (folders made as needed); answers `{"file": path, **meta}` |
| `lab.table(path, rows)` | Writes a list of dicts as CSV; answers `{"file": path, "rows": n}` |
| `lab.changes(path)` | Writes the writes made through this `Lab` as JSON (below) and answers it |
| `lab.request(method, path, body)` | Any other InstruCore route, with the same headers, record and errors |

## What holds a machine

| `lab.machine(…)` names | In the `with` block |
| --- | --- |
| A simulated pool (`signal-bench-sim`) | A free copy is held for the block. When none is free the block waits in the pool's queue, asking again when a copy is offered; `wait_s` gives up and leaves the queue. The copy is released at the block's end, also when the block raises, and reset for the next user |
| One simulated copy (`signal-bench-sim-2`) | That copy, the same way |
| A live machine (`signal-bench`) | Nothing is held. The member holds a live machine, in the conversation; the lab answers a script's session there with `RUN_REQUIRED` and a write the member did not allow with `APPROVAL_REQUIRED` |

Code holds a live machine only as a run the member approved (live runs in the lab's queue come in a later
version). Rehearse on a copy first.

## The record and `changes.json`

`record` (a path) gets one JSON line per call, appended: `time`, `method`, `path`, `args`, `ok`, `status`,
`code` (the error code, or null), `message` on a refusal, `req_id`, and for an op call `machine`, `role`,
`instrument`, `kind` and `op`. A successful call's line carries its `result`, or `file` when the script saved that
result with `lab.save` straight after the call. The line of a call that answered a dict or a list is written with
the next call (or at the block's end), so it can name the file.

`lab.changes(path)` groups the writes by role and op: one numeric argument that moved is a range, several a box,
the rest fixed. `channel` never moves: each channel is its own change.

```json
{"script": "experiments/2026-11-21-frequency-response/run.py@3f2a91c", "machines": ["signal-bench-sim-1"],
 "changes": [
  {"role": "generator", "op": "set_waveform", "calls": 7, "quantity": "frequency_hz", "min": 100, "max": 100000.0,
   "fixed": {"channel": 1, "shape": "sine", "amplitude_v": 1.0}},
  {"role": "scope", "op": "set_timebase", "calls": 7, "quantity": "scale_s", "min": 5e-06, "max": 0.005,
   "fixed": {"offset_s": 0.0}}]}
```

A box reads `"ranges": {"x": [min, max], "y": [min, max]}` in place of `quantity`, `min` and `max`.

Every request sends `Authorization: Bearer <key>` and `X-InstruCore-Script: <script>@<commit>`: the running
script's path in its git repository and the short commit, so the lab's record shows the code behind each call.
Outside a git repository the header is left out; `Lab(…, script_ref="…")` sets it, `script_ref=""` sends none.

## Errors

A refusal raises `LabError`, with `code`, `message` (the lab's sentence as it sent it), `detail` and `status`
(the HTTP status, 0 when no answer came). It reads InstruCore's `{"error": {code, message, detail}}` and the QuPai
gateway's problem documents (`urn:qupai:problem:rate` becomes `RATE`, `invalid-key` `INVALID_KEY`). A 429 waits
for `Retry-After` (2 s without one) and asks again, up to 5 times.

| Code | What it means |
| --- | --- |
| `LIMIT_EXCEEDED`, `POLICY_DENIED`, `FORBIDDEN` | The lab refuses this value or op. Save what you have and report the limit; never retry with another value or through another op |
| `APPROVAL_REQUIRED` | A write on a live machine the member has not allowed |
| `RUN_REQUIRED` | A script asked to hold a live machine: only a run the member approved holds one |
| `SESSION_REQUIRED`, `MACHINE_IN_SESSION` | The write needs your session on that machine, or someone else holds it |
| `INVALID_KEY`, `FORBIDDEN`, `RATE`, `UNAVAILABLE` (the gateway's) | The key, your role on the lab, too many calls, the lab not reachable through QuPai |
| `UNREACHABLE`, `NOT_CONFIGURED` | No answer from the address; no address or key found |
| `UNKNOWN_ROLE`, `NOT_HELD`, `WAIT_TIMEOUT`, `REQUEST_EXPIRED`, `REQUEST_CANCELLED` | The SDK's own: no such role, the machine used outside `with`, no copy offered in `wait_s`, the queue request ended |

## Tests

```
cd sdk/python
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/pytest -q
```

They run against a fake InstruCore in a thread. `tests/test_integration.py` also runs against a real InstruCore
with a simulated pool when `QUPAI_LAB_IT_URL` and `QUPAI_LAB_IT_KEY` (a read-write key) are set;
`QUPAI_LAB_IT_POOL` picks the pool.

## License

MIT
