# QuPai Lab for Claude Code

Work with your [QuPai](https://qupai.quspect.com) lab from Claude Code: the lab's instruments as tools, a skill that
says how to operate them safely, and one command that opens the lab's live page.

| Part | What it is |
| --- | --- |
| The `lab` tools | Your lab through QuPai's gateway: `machines`, `describe`, `read`, `write`, `sweep`, sessions, simulated copies, `stop`. QuPai checks your role on the lab and the lab's limits on every call |
| `lab-operator` skill | How Claude works on a lab: look first, rehearse on a simulated copy, ask before writing to a real machine, never get around a limit, `stop` when something looks wrong |
| `/qupai-lab:watch` | Opens the lab's live page (Instruments, live) in your browser |

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

What your role on the lab allows: with view, everything on the simulated copies and reading, watching and `stop` on
the real machines; with operate, also holding a real machine and writing to it. The lab's admin may hold agents
outside QuPai, such as Claude Code, to view. Every call shows on the lab's live page, marked as an agent's.

## Privacy

The plugin holds no secrets and sends nothing anywhere itself. Claude Code calls your QuPai deployment with your
sign-in, under that deployment's terms.

## License

MIT
