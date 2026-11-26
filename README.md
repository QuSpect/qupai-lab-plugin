# QuPai Lab for Claude Code

Work with your [QuPai](https://qupai.quspect.com) lab from Claude Code: the lab's instruments as tools, a skill that
says how to operate them safely, and one command that opens the lab's live page.

| Part | What it is |
| --- | --- |
| The `lab` tools | Your lab through QuPai's gateway: `machines`, `describe`, `read`, `write`, `sweep`, `measure` (a whole scan in one call, InstruCore 0.42.0), sessions, simulated copies. QuPai and the lab check your role on each bench and the lab's limits on every call |
| `lab-operator` skill | How Claude works on a lab: look first, rehearse on a simulated copy, ask before writing to a real machine, never get around a limit, stop and tell you when something looks wrong |
| `/qupai-lab:watch` | Opens the lab's live page (Instruments, live) in your browser; `/qupai-lab:watch <bench>` opens one bench |
| The Lab SDK | `qupai-lab`, a Python package in [`sdk/python`](sdk/python/README.md) for scripts that work the lab through InstruCore's REST API: simulated copies held for a block, instruments by role, a record of every call, the changes for a live run's card (rehearsed, or declared for a lab with no simulated copy), and live runs in the lab's queue (`qupai-lab run`), and a Moku through the lab with the manufacturer's own `moku` package (`m.moku()`). It runs in a QuPai lab project, at the lab PC, and outside QuPai with your own `qupai` sign-in on a lab QuPai staff granted you (`Lab.from_qupai("<lab>")`, through a lab workstation's door there) |

## Install

In Claude Code:

```
/plugin marketplace add QuSpect/qupai-lab-plugin
/plugin install qupai-lab@qupai-lab
```

It asks for three settings. Your lab's page in QuPai shows the address under *Use in Claude or another agent*:
`https://<gateway>/mcp/lab-<lab id>`.

| Setting | Example |
| --- | --- |
| QuPai web address | `https://qupai.quspect.com` (the default) |
| QuPai gateway address | The part before `/mcp/` |
| Lab id | `csmot`: the part after `/mcp/lab-` |

To change them later: `/plugin configure qupai-lab@qupai-lab`. From a shell:

```
claude plugin marketplace add QuSpect/qupai-lab-plugin
claude plugin install qupai-lab@qupai-lab --config gateway=https://<gateway> --config lab=<lab id>
```

Then sign in once: `/mcp`, choose `plugin:qupai-lab:lab`, Authenticate. QuPai's own sign-in opens in your browser.
Claude Code keeps the sign-in; QuPai decides what it allows.

## What Claude can do there

One lab PC may serve several benches of the lab, each a real machine of its own with its own instruments: `machines`
lists them by id (a lab with one bench calls it `bench`), and Claude names the one it works on. Your role is per
bench, as the lab's admin set it: on a bench where you hold view, everything on its simulated copies and reading and
watching the real machine; with operate, also holding it and writing to it, which is also how an instrument is
turned off: InstruCore has no machine-wide stop. A bench you hold no role on is not there for you. The lab's admin
may hold agents outside QuPai, such as Claude Code, to view. Every call shows on the lab's live page, marked as an
agent's.

## Privacy

The plugin holds no secrets and sends nothing anywhere itself. Claude Code calls your QuPai deployment with your
sign-in, under that deployment's terms.

## License

MIT
