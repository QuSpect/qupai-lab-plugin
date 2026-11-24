# Changelog: the Lab SDK (`qupai-lab`)

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
