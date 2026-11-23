# QuPai Lab SDK (`qupai-lab`)

Work a QuPai lab's instruments from Python: the lab's machines, sessions on simulated copies, instruments by
role, the files an experiment keeps, and live runs in the lab's queue. It calls InstruCore's own REST API (not
MCP), so one script runs in a QuPai lab project's sandbox and at the lab PC. Python 3.10 or later, standard
library only.

## Install

```
pip install qupai-lab
```

A QuPai sandbox has it already. From a checkout of this repository instead: `pip install ./sdk/python`.

**Releasing.** A tag `sdk-vX.Y.Z` on this repository, equal to `qupai_lab.__version__`, runs
`.github/workflows/sdk-release.yml`: it tests the SDK on Python 3.10 and 3.13, builds the sdist and the wheel, checks
the tag against them, and publishes to PyPI through Trusted Publishing (no token is kept anywhere).

Then check that this sandbox (or PC) reaches its lab:

```
$ qupai-lab machines
Lab http://door.example/lab/lab-sim
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

lab = Lab.from_env(record="calls.jsonl")      # this run's folder: <experiment>/runs/<id>/
sweep = lab.input("sweep.json")                # inputs/sweep.json: {"frequencies_hz": [100, 300, …]}
with lab.machine(sys.argv[1]) as m:            # a pool: holds a free copy, or waits in its queue for one
    gen, scope = m.role("generator"), m.role("scope")
    rows = []
    for f in sweep["frequencies_hz"]:
        gen.write("set_waveform", channel=1, shape="sine", frequency_hz=f, amplitude_v=1.0)
        scope.write("set_timebase", scale_s=0.5 / f, offset_s=0.0)
        trace = scope.read("acquire", channel=1, points=2000)
        rows.append({"frequency_hz": f, **lab.save(f"data/raw/trace_{f:g}.json", trace)})
lab.table("data/response.csv", rows)
lab.changes("changes.json")                    # the writes above, for a live run's card
```

`python run.py signal-bench-sim` runs it on a simulated copy; its files land in its own run folder (below). The roles are the machine's: `qupai-lab machines`
lists them. A script addresses instruments by role, so the same script works on any copy of a pool and on the
machine the pool simulates, when their roles have the same names.

**Name the bench.** One lab PC may serve several benches of the lab, each a live machine of its own (with its own
instruments, holder and queue) named by its public id, such as `rydberg-a`; a lab with only one bench calls it
`bench`. `lab.machine()` always takes the id: there is no default machine, so take it from the command line, as
above, and never write `bench` into a script that may meet another lab. `lab.machines()` lists only the benches
the key's member holds a role on; another bench answers `NOT_FOUND`.

| Call | What it does |
| --- | --- |
| `lab.machines()` | Each live machine (a bench) and simulated copy the member may see: `id` (public id), `internal_id`, `name`, `kind`, `state`, `status`, `pool`, `roles` (role → instrument id) |
| `lab.machine(id, *, purpose="", wait_s=None)` | A machine for a `with` block (below), by id or public id; there is no default |
| `m.role(name)` | The instrument in that role |
| `m.moku(device)` | The keyword arguments for the official `moku` package on that Moku of the machine, through the lab ([below](#a-moku-with-the-official-sdk)) |
| `inst.read(op, **args)` | A read op; answers its result. Anyone may read |
| `inst.write(op, **args)` | A write op in the machine's session; the lab checks its limits first |
| `inst.check(op, **args)` | Would this write pass policy, arguments and limits? `{"ok": true, "kind": "write"}` or the refusal, without a session |
| `inst.reference()`, `m.reference()` | The reference document (`mhs-reference/0`): ops, units, limits, notes |
| `m.renew()` | Restarts the held session's time limit, unless someone waits |
| `lab.save(path, value, **meta)` | Writes `value` as JSON (folders made as needed); answers `{"file": path, **meta}` |
| `lab.table(path, rows)` | Writes a list of dicts as CSV; answers `{"file": path, "rows": n}` |
| `lab.changes(path)` | Writes the writes made through this `Lab` as JSON (below) and answers it |
| `lab.input(name)` | `inputs/<name>` of the experiment: parsed when it is `.json`, else its text |
| `lab.out`, `lab.experiment`, `lab.inputs` | This run's folder, the experiment's folder, its `inputs/` (paths) |
| `lab.request(method, path, body)` | Any other InstruCore route, with the same headers, record and errors |

## Where a script's outputs go

Each execution of an experiment's script has its own **run folder**, `<experiment>/runs/<id>/`. A live run's id is
its job; a rehearsal's is the UTC time it started and four random hex digits (`20261122-021116-a3f9`), so the
folders sort by time. `Lab.from_env()` makes it for a script run as `python run.py …`, at the script's first call
or output, and a relative path given to `record`, `lab.save`, `lab.table` or `lab.changes` is inside it. The
experiment's own folder keeps only what a person writes, and the latest rehearsal's `changes.json`, which a live
run's card reads:

```
experiments/
├── INDEX.md                          every experiment and its runs, from their manifests (written, not edited)
└── 2026-11-22-freq-response/
    ├── README.md                     what was asked, why, how to run it again, what the runs found
    ├── run.py                        the steps: python run.py <machine>
    ├── analysis.py                   tables and figures of one run: python analysis.py runs/<id>
    ├── inputs/sweep.json             the values run.py reads (lab.input)
    ├── changes.json                  the latest rehearsal's changes: the live run's card
    └── runs/
        ├── 20261122-021116-a3f9/     a rehearsal, on a simulated copy
        │   ├── manifest.json
        │   ├── calls.jsonl  changes.json
        │   └── data/response.csv  data/raw/ (not committed)  figures/
        └── 01a20106-20c6-…/          a live run, its job's id: the same files
```

`manifest.json` says what ran and how it ended: `run`, `kind` (`rehearsal` or `live`), `simulated` (every machine
held was a simulated copy), `experiment`, `script` and `script_blob` (its git blob id), `argv`, `commit` and
`dirty` (the experiment differed from the commit, `runs/` aside), `inputs` (each file of `inputs/` with its size and
blob id), `lab` (the listing, from a sandbox door's address; no address is kept), `machines` (each one used:
`requested`, `machine`, `kind`, `session`, `held`, `held_at`, `released_at`), `official_sdk` (each device worked with
a manufacturer's own package, [`m.moku()`](#a-moku-with-the-official-sdk): `package`, `version`, `machine`, `device`;
the lab records those calls), `started_at`, `ended_at`, `status`
(`running`, `ended`, `failed` after an uncaught exception, `stopped` by a signal), `exit_code` (a live run's),
`calls`, `outputs` (the files the run itself left, the raw ones counted in `raw_files`; an analysis adds its own
later) and `versions`.

`experiments/INDEX.md` lists each experiment (newest first: its date, from the folder's name, and its README's
title, without a date of its own) and each of its runs: kind,
machine, commit (`*`: it differed), start, status and calls; a run folder without a manifest (from an SDK before
0.3.0) is listed by name. A rehearsal writes it as it ends, `qupai-lab run`
after a live run, and `qupai-lab index` on demand. A script outside a folder named `experiments` leaves it alone.

`Lab.from_env(runs=False)`, or `QUPAI_LAB_RUNS=0`, makes no run folder: paths are then as given, from the working
directory. `python -c`, the interactive interpreter and test runners get none either, and `Lab(url, key)` gets one
only when given (`run_folder=`).

## What holds a machine

| `lab.machine(…)` names | In the `with` block |
| --- | --- |
| A simulated pool (`signal-bench-sim`) | A free copy is held for the block. When none is free the block waits in the pool's queue, asking again when a copy is offered; `wait_s` gives up and leaves the queue. The copy is released at the block's end, also when the block raises, and reset for the next user |
| One simulated copy (`signal-bench-sim-2`) | That copy, the same way |
| The run's live machine, inside a [live run](#live-runs) (`QUPAI_LAB_RUN_MACHINE`, by id or public id) | Held for the block: `POST sessions` with `{"purpose": "run <run id>", "queue": true}`. While others hold or wait for it, the block waits in the machine's queue and asks again when it is offered; the lab holds it for the run's longest time. Released at the block's end, as a copy is |
| Any other live machine (`signal-bench`) | Nothing is held. The member holds a live machine, in the conversation; the lab answers a script's session there with `RUN_REQUIRED` and a write the member did not allow with `APPROVAL_REQUIRED` |

Code holds a live machine only as a run the member approved. Rehearse on a copy first.

SIGTERM and SIGINT while a block holds a machine (a copy, or a run's live machine) release it before the script
exits: the first one raises `SystemExit(143)` (SIGTERM) or `KeyboardInterrupt` (SIGINT) in the block, the block's
end releases the session with the reason `qupai-lab: stopped by SIGTERM`, and a request still in the queue is
withdrawn. A signal that comes while a session starts or ends waits until that call is done; a repeat while the
script stops is ignored. The handlers are installed when the block starts and the script's own are back at its
end (a signal the script ignores stays ignored). Only the main thread installs them.

## A Moku with the official SDK

A lab's Moku can be worked with the manufacturer's own Python package, `moku`, as at the device: code written
against a Moku runs unchanged but for its address, and every call goes through the lab. `m.moku(device)` gives
the keyword arguments for any of the package's instruments, `MultiInstrument` and `Moku`, on the Moku `device` of
the machine in the block:

```python
import sys
from moku.instruments import SpectrumAnalyzer
from qupai_lab import Lab

lab = Lab.from_env()
with lab.machine(sys.argv[1]) as m:
    with SpectrumAnalyzer(**m.moku("moku")) as sa:   # hands the Moku back at the block's end
        sa.set_span(frequency1=0, frequency2=10e6)
        spectrum = sa.get_data()
    lab.save("data/spectrum.json", spectrum)
```

`MultiInstrument(**m.moku("moku"), platform_id=4)` and its `set_instrument` work the same way. `m.moku()` calls
nothing; it answers three arguments:

| Argument | Value |
| --- | --- |
| `ip` | The lab's address without `http://`, then `/v1/moku/<device>`. The package builds `http://{ip}/api/<group>/<operation>`, so its calls reach `<QUPAI_LAB_URL>/v1/moku/<device>/api/…`, where InstruCore answers the Moku's own HTTP API behind its checks |
| `session_headers` | The headers of this `Lab`'s own calls: `Authorization: Bearer <key>`, `X-InstruCore-Script` (when the script has one) and `User-Agent`. The package makes them its HTTP session's headers and adds its `Moku-Client-Key`, so each call gives a new dict |
| `manage_bitstreams` | `False`: the package uploads no FPGA bitstreams, since InstruCore uploads what the Moku lacks at the lab PC |

- **The lab checks every call** as it checks this SDK's. The key tells the lab whose calls they are, so they belong
  to whatever this `Lab` holds: the block's copy, or a run's live machine. Reads pass; writes need the hold, and on a
  live machine the lab's limits and the member's approval. The package raises its own exceptions
  (`moku.exceptions.MokuException` and its kinds), never `LabError`, and it does not wait and ask again on a `429`
  as this SDK does.
- **Ownership.** Each instrument's constructor claims the Moku (`claim_ownership`); the lab grants it only while
  this key holds the machine, and `force_connect` takes the Moku from no one. Hand it back with
  `relinquish_ownership()`, or with a `with` block as above.
- **The lab keeps the record.** InstruCore records every call of the package. The `record` file, the manifest's
  `calls` and `lab.changes()` count and list this SDK's own calls only; the manifest's `official_sdk` (and
  `changes.json`, when there is any) names each device worked this way: `package`, `version`, `machine`, `device`.
- **http only.** The package builds `http://` URLs itself, so the lab must be at an http address, as a QuPai
  sandbox's door is; an https address raises `LabError` `HTTP_ONLY`. A device id is letters, digits, `_` and `-`
  (`INVALID_DEVICE` otherwise).
- **Not through the lab:** streaming (`start_streaming`, `get_stream_data`, `stream_to_file`) runs the
  manufacturer's `mokucli` against the device itself. `import moku` warns that it cannot find `mokucli`; nothing
  above needs it.
- The package's other arguments keep their defaults and may be given beside: `SpectrumAnalyzer(**m.moku("moku"),
  read_timeout=60)`. The lab needs an InstruCore that offers `/v1/moku/<device>`; an older one answers `404`, which
  the package reports as "Method not found".

`moku` is not a dependency of `qupai-lab`, which only builds its arguments: a QuPai sandbox has it, and
`pip install "qupai-lab[moku]"` installs the version this SDK is tested with (4.3.0.1, MIT).

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

With a run folder the answer starts with `run`, the run's id; a rehearsal also writes it to
`<experiment>/changes.json`, where a live run's card reads it. A script that worked a Moku with the official package
gets `official_sdk` after `machines` (the devices, as in the manifest): those writes are in the lab's record, not
among the changes.

Every request sends `Authorization: Bearer <key>` and `X-InstruCore-Script: <script>@<commit>`: the running
script's path in its git repository and the short commit, so the lab's record shows the code behind each call.
Outside a git repository the header is left out; `Lab(…, script_ref="…")` sets it, `script_ref=""` sends none.

## Live runs

When the member approves a live run (QuPai's `lab_run`, or Run on an experiment folder), QuPai writes the run's key
file into the place's repository and starts, in the repository's root:

```
qupai-lab run --key-file .qupai/runs/<run>/key.env --commit <sha> experiments/2026-11-21-freq signal-bench
```

1. It reads `QUPAI_LAB_URL`, `QUPAI_LAB_KEY` (the run's key), `QUPAI_LAB_RUN` (the run's id) and
   `QUPAI_LAB_RUN_MACHINE` from the key file (`KEY=VALUE` lines) and deletes it at once, whatever it holds. The
   machine must be the one named on the command line.
2. It checks the commit out into `/tmp/qupai-run-<run>` (`git worktree add --detach`, in the repository found from
   the working directory). Any committed `data/`, `figures/`, `calls.jsonl` and `changes.json` beside `run.py` (a
   rehearsal's, from before run folders) are removed from that checkout, so what the run keeps is its own.
3. It runs `python run.py <machine>` in `<checkout>/<experiment>` with the Python it runs in, with the four
   settings in place of any other `QUPAI_*` variable and `PYTHONUNBUFFERED=1`. The script's output is the
   command's own, as it is printed. The script's `Lab.from_env()` gets the run's key and its run folder
   (`runs/<run>/`), and `lab.machine(<machine>)` holds the live machine (above).
4. When the script ends it copies the run folder, and any `data/`, `figures/`, `calls.jsonl` and `changes.json`
   the script wrote beside `run.py` itself, to `<experiment>/runs/<run>/` in the repository. The manifest gets
   the approved commit, the exit code and the status from it (a short one is written when the script left none),
   `experiments/INDEX.md` is written again, and the checkout is removed, also when the script failed.
5. It exits with the script's exit code (128 + the signal's number when a signal ended the script).

SIGTERM and SIGINT are passed to the script, which releases the machine and exits; a script that has not ended
20 s later is killed. What it left is still copied and the checkout removed. A run that cannot start (a key file
without the four settings, another machine, no repository, no such commit, no `run.py` in the experiment at that
commit) says why on stderr and exits with 2, after deleting the key file.

### Points for QuPai (`QUPAI_METRIC`)

Inside a run (`QUPAI_LAB_RUN` set) the SDK prints the run's progress on stdout as lines QuPai's Runtime takes from
a background job's output, one point each:

```
QUPAI_METRIC {"key": "queue_position", "value": 2}
QUPAI_METRIC {"key": "held", "value": 1}
QUPAI_METRIC {"key": "calls", "value": 14}
QUPAI_METRIC {"key": "held", "value": 0}
```

| Key | When |
| --- | --- |
| `queue_position` | The request's position in the machine's queue, when it is queued and each time it moves |
| `held` | 1 when the session starts, 0 when it is released |
| `calls` | The instrument calls made so far (reads, writes and checks, refused ones too), at most every 5 s, and when the session is released and the script exits |

Outside a run nothing is printed on stdout.

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
| `NOT_FOUND` | No such machine or instrument, or a bench you hold no role on |
| `UNREACHABLE`, `NOT_CONFIGURED` | No answer from the address; no address or key found |
| `UNKNOWN_ROLE`, `NOT_HELD`, `WAIT_TIMEOUT`, `REQUEST_EXPIRED`, `REQUEST_CANCELLED` | The SDK's own: no such role, the machine used outside `with`, no copy offered in `wait_s`, the queue request ended |
| `INVALID_DEVICE`, `HTTP_ONLY` | `m.moku()`'s own: not a plain device id; the lab's address is not plain http, which the official `moku` package needs |

## Tests

```
cd sdk/python
python3 -m venv .venv && .venv/bin/pip install -e ".[moku]" pytest
.venv/bin/pytest -q
```

They run against a fake InstruCore in a thread; `tests/test_moku_official.py` drives the official `moku` package
through `m.moku()` against the fake's stand-in for InstruCore's Moku endpoint, and is skipped when the package is not
installed (`pip install -e . pytest` without the extra); `qupai-lab run`'s tests make git repositories under the test's
temporary folder and checkouts in `/tmp/qupai-run-<run>`, and send signals to scripts in subprocesses. `tests/test_integration.py` also runs against a real InstruCore
with a simulated pool when `QUPAI_LAB_IT_URL` and `QUPAI_LAB_IT_KEY` (a read-write key) are set;
`QUPAI_LAB_IT_POOL` picks the pool.

## License

MIT
