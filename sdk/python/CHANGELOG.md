# Changelog: the Lab SDK (`qupai-lab`)

## 0.6.2

On a lab workstation, 2026-11-26, QuPai's gateway answered every call `429 Too Many Requests` ("too many requests
from this address", `Retry-After: 60`) for about 30 s after a burst of calls; a script working the Magnetic bench
raised, and its clean-up failed too. Another run printed nothing for 8 minutes while it waited, until it was killed.

- The platform's `429`, and a `503` that names a `Retry-After`, is waited for and the same call sent again when that
  is safe: every GET (the machine list, a machine, a queue request), an instrument's `read` and `check` ops, a
  session's start, renewal and end, withdrawing a queue request; and a write after the gateway's own `429`
  (`urn:qupai:problem:rate`), which it answers before the call reaches the lab (qupai-gateway's REST door: the
  address's calls per minute and the member's calls to the lab; InstruCore answers no `429`). A write refused any
  other way is never sent twice. Each wait is the `Retry-After` named (seconds or an HTTP date), else 2 s doubling;
  at least 1 s and at most 60 s each, 5 minutes in all per call (it was 5 tries, 2 s apart without a header, and a
  write was sent again after any `429`). The first wait of a call is said once on stderr ("rate-limited by the
  platform; waiting 1 min, then asking again (…)"), and so is giving up. The record's line gets `retries` and
  `waited_s`, the raised error's `detail` too, and the manifest counts the waits (`rate_limited`). A signal while a
  session's start waits stops the script at once (`stop.unshielded`): nothing was started.
- `Lab.from_qupai`'s first question (`GET /v1/me/lab-access`) waits the same way; it raised at the first `429`.
- The official `moku` package's calls (`m.moku()`): the stand-in waits for the gateway's `429` and sends the call
  again while the waits fit in 20 s, under the package's own read timeout, and while the package still waits;
  otherwise the package gets the refusal in the Moku's envelope (`RATE`, with the platform's sentence) instead of
  "Unknown exception. Status code: 429", said once on stderr.
- A block that waits in its machine's queue says so: on stderr, what it waits for when it starts waiting (who holds
  the machine and since when, and when its session ends at the latest, or how many copies of a pool are free; its
  place in the queue), a line when its place moves, `still waiting for <machine> (4 min 30 s): position 1; held by …`
  every 30 s (the holder looked up again), and when it holds the machine. The manifest keeps each wait in `waits` as
  it goes (`machine`, `for`, `since`, `position`, `holder`, `lab_said`, `waited_s`, `until`, `outcome`). Its looks at
  the queue keep their backoff (2 s, then up to every 15 s while its place does not move).
- The README says how to start a long run on a lab workstation detached (`lab-detach`, else `nohup` in its own
  session), follow its log and stop it with SIGTERM; so does the `lab-operator` skill (plugin 0.1.5).

## 0.6.1

- A live run whose session ended under it (the member released the bench from the bench page, or the lab ended it
  after its idle limit) puts back what its clean-up can. A call on the run's machine that the script makes while it
  stops (in a `finally`, an `except` or a `with` block's exit, as `RunSessionEnded` or its own error goes by) goes
  through a session the run takes for that, `{"purpose": "run <id>: putting back its settings", "queue": false,
  "max_s": 120}`, once and only while the machine's block is open, the machine is free, nobody waits for it and the
  run's longest time is not over; the block's end ends it. The lab checks the run's key and its card as in the run.
  Otherwise the call reaches nothing and raises `LabError` `NOT_PUT_BACK` saying why (who holds the machine now,
  someone waiting, the run's longest time, the block already ended), so the script's own `except LabError` goes on
  with the rest of its clean-up. A script that swallows the end and carries on outside a handler still reaches
  nothing (`RunSessionEnded`, as in 0.6.0). On Magnetic, 2026-11-26, a run's `finally` could not put the Moku's input
  back (`BENCH_NOT_HELD`) and left it on DC / 400 mVpp, twice.
- The manifest's `restore` says what became of the run's settings: the `session` taken to put them back (or
  `why_not`), each change of the clean-up after the end (`after_end`, with `put_back` and, when it did not reach the
  instrument, `why`; the official `moku` package's calls the stand-in refused that would have changed something are
  listed too), and `left_set`, what the run set on the machine and did not put back (the last write of each setting,
  by role, op and the argument that says which: `channel`, `slot`, `output`, …). Its `error`, and so the run's answer
  and job, adds a sentence on each: what was put back, what did not reach the machine and why, and what is still set.

## 0.6.0

- A live run tells QuPai `held_since` (when its session started, as the lab says) with `held` 1, so the run's job
  says it holds the bench since then. `held` and `queue_position` are the run's own machine's only: a simulated copy
  the run's script holds no longer toggles them.
- A live run whose session ended while it ran, not by the run (the member released it from the bench page on
  2026-11-26 while the run wrote; the run carried on into refusals), now stops at once. On the lab's refusal
  (`BENCH_NOT_HELD`, `SESSION_REQUIRED`, `BENCH_HELD`, `MACHINE_IN_SESSION`, also for the `moku` package's calls)
  the SDK looks at the machine, and when the session is no longer the run's it says why (its longest time, its idle
  limit, or someone other than the run; when the lab refused; who holds it now), keeps that as the manifest's `error`,
  tells QuPai `held` 0 and `session_ended`, and raises `RunSessionEnded` (a `SystemExit`, exit status 1, in the main
  thread for the `moku` package's calls). Later calls raise it again without reaching the lab; the block's end ends
  nothing. `qupai-lab run` fails such a run even when the script caught it and exited 0.
- `qupai-lab run` prints the run's answer as its last line, `{"run", "status", "exit_code", "error"}`, which QuPai's
  Runtime hands the run's job as its output: the job then says why a run failed or could not start.
- `declare_changes(experiment, changes)` writes `<experiment>/changes.json` with a live run's changes declared
  rather than rehearsed (`"declared": true`), checked against what the card takes (`INVALID_CHANGE`): for a lab with no
  simulated copy, whose run's card was empty and whose first write the lab refused (`APPROVAL_REQUIRED`, Magnetic,
  2026-11-26).

## 0.5.0

- `Lab.from_qupai("magnetic")` works a lab outside QuPai with the person's own `qupai` sign-in, with no `.env`
  (direct lab access, DL8). It reads the sign-in `qupai login` keeps (`QUPAI_CREDENTIALS`, else
  `qupai/credentials.json` in the user's configuration directory: its `server` and `api_key`; the key is never
  printed), asks the platform which labs the person may work directly from where they are
  (`GET /v1/me/lab-access`), and speaks InstruCore's REST API at `<platform>/rest/<listing>/` with the person's key
  (`QUPAI_GATEWAY` names another gateway). A lab not listed raises `NO_DIRECT_ACCESS`, naming the labs the person may
  work directly and saying that asking the lab's agent is the way for the others; no sign-in, or a device sign-in
  without a key, raises `NOT_SIGNED_IN`; a platform without the route, `NOT_OFFERED`.
- On a lab workstation every call goes through the workstation's door, HTTP over the Unix socket
  `QUPAI_WORKSTATION_LAB_SOCKET`, else `/Library/QuPai/workstation/lab.sock` when it exists, `m.moku()`'s included.
  `Lab(url, key, unix_socket=…)` sends any `Lab`'s requests through such a socket.
- A lab worked directly holds a live machine in `with lab.machine(…)` as the person's own session
  (`{"purpose": …, "queue": true}`, waiting in the queue), so the same experiment code runs on the workstation and
  as a live run in a QuPai sandbox. A session the person already held when the block began stays held at its end.
  `Lab(…, direct=True)` asks for the same. In a sandbox nothing changes: a live machine is held only for its run.
- `qupai-lab machines --lab <lab>` lists a lab worked this way. The manifest's `lab` names the listing of a
  `/rest/<listing>` address too.

## 0.4.2

- `m.moku(device)` works with the official `moku` package 4.4.0.0 as well as 4.3.0.1. The package now talks to a
  local stand-in for the Moku (`qupai_lab.moku_forward`, one per device until the block ends), which passes every
  call on to `<lab>/v1/moku/<machine>/<device>/api/…` with this Lab's key, script header and user agent and keeps the
  writes the lab lists. 4.4's fast transport kept only the host and port of the address it was given and sent no
  headers, so its calls missed the lab (`MissingApiServer` on the Magnetic lab's MokuOS 4.4 Pro, 2026-11-24).
  `m.moku()` answers `ip` (`127.0.0.1:<port>`), `manage_bitstreams=False`, `session_trust_env=False` and
  `session_proxies={}`; `session_headers` and `session_hooks` are gone. It sets `MOKU_API_DISABLE_BITSTREAM_MANAGEMENT=1`,
  which 4.4 reads in place of `manage_bitstreams`. Needs InstruCore 0.27.1 for 4.4's calls (its `client_name` and
  its two questions before a deploy).
- A lab at an https address works too (the stand-in makes the call); `HTTP_ONLY` is gone, an address that is not
  http(s) raises `INVALID_URL`. `moku_address` is now `moku_target` (the full URL of the lab's endpoint).

## 0.4.1

- A rehearsal's `changes.json` lists the writes code made with the official `moku` package (`m.moku()`), so a live
  run's card can approve them. Before, it left them out and every such write of a live run was refused
  `APPROVAL_REQUIRED`. `m.moku()` gives the package a response hook (`session_hooks`), which reads the writes the
  lab lists in each answer (`instrucore.writes`: instrument, op and arguments, exactly as the lab matches a run's
  permission; InstruCore 0.26.2 on) and keeps them under the instrument's role on the machine. A lab whose answers
  list none is said once on stderr, and `official_sdk` gets `"writes_listed": false`.
- `qupai-lab run` keeps the script's stdout and stderr as the run's `output.log` (the first MiB), still passing them
  on as before, and a run that did not end well gets the last lines of its stderr in the manifest (`error`).
  Before, a failed run's folder did not say why it failed.

## 0.4.0

- `m.moku(device)`: the keyword arguments for the official `moku` package on a Moku of the machine in the block,
  so its instruments, `MultiInstrument` and `Moku` work through the lab as at the device:
  `SpectrumAnalyzer(**m.moku("moku"))`. `ip` is the lab's address without `http://` plus
  `/v1/moku/<machine>/<device>`, where `<machine>` is the machine in the block by its public id (the live machine, or
  the simulated copy held); `session_headers` are this `Lab`'s `Authorization`, `X-InstruCore-Script` and
  `User-Agent`, a new dict each call; `manage_bitstreams=False`. It calls nothing. A lab at an https address raises `HTTP_ONLY` (the package speaks
  http only); a machine or device id that is not letters, digits, `_` and `-` raises `INVALID_MACHINE` or
  `INVALID_DEVICE`.
- The run's manifest names each device worked this way (`official_sdk`: `package`, `version`, `machine`, `device`)
  and the package's version (`versions.moku`); `changes.json` carries the same when there is any. The lab records
  the package's calls; this SDK's record, `calls` and changes do not.
- The extra `qupai-lab[moku]` installs `moku==4.3.0.1`; `qupai-lab` itself stays standard library only.

Earlier versions: the repository's history (`git log -- sdk/python`).
