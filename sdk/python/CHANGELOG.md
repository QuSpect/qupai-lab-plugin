# Changelog: the Lab SDK (`qupai-lab`)

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
