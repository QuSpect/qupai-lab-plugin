---
name: lab-operator
description: Operate a QuPai lab's instruments through this plugin's lab tools (machines, describe, read, write, sweep, sessions, stop). Use when the user asks to look at, measure with, rehearse on or control the instruments of their QuPai lab.
---

# Operating a QuPai lab

This plugin's `lab` server is the user's QuPai lab: QuPai's gateway in front of the lab PC's InstruCore, the program
that drives the lab's instruments. Every call is checked there: the user's role on the lab, the lab's limits, and who
holds each machine.

What the user's role allows:

| Role | Simulated copies | Real machines |
| --- | --- | --- |
| View | Everything | Read, watch live, `stop` |
| Operate | Everything | Also hold a machine in a session and write to it |
| Admin | Everything | Also end someone else's session |

The lab's admin decides how far an agent outside QuPai goes: at view, you hold the view role here whatever the
user's own role is.

## Rules

1. **Look first.** Call `machines`, then `describe` the machine or instrument before acting: its operations, units,
   limits and safe state.
2. **Rehearse on a simulated copy.** `start_simulated_session` with the machine's pool gives a fresh copy with the same
   instruments, operations and limits and no hardware. End it with `end_session` when done.
3. **Ask before a real write.** Before anything that changes a real machine, tell the user exactly what you will
   write, and wait for their yes. Writes need a session: `start_session` (it may put you in a queue). Renew it only
   while working, and end it when done.
4. **Limits hold for everyone.** You get the lab's stricter agent limits. On `LIMIT_EXCEEDED`, report the limit and
   the value; never nudge a value to get past a limit.
5. **Stop when something looks wrong.** `stop` puts a machine in its safe state, for anyone, at any time. Say that
   you did it and why.
6. **Say where each number came from:** the real machine, or a simulated copy.
7. **The user can watch.** `/qupai-lab:watch` opens the lab's live page in QuPai, where every call shows in the
   activity strip, yours marked as an agent's.

## When a call is refused

| Error | What it means |
| --- | --- |
| `SESSION_REQUIRED`, `BENCH_NOT_HELD` | Start a session on that machine first |
| `BENCH_HELD`, `MACHINE_IN_SESSION` | Someone else holds it; reads still work. Offer to join the queue |
| `QUEUE_WAITING`, `SESSION_PENDING` | You are in the queue; it says where |
| `FORBIDDEN` with `required` | The user's role, or the lab's setting for outside agents, does not allow it. Tell the user |
| `LIMIT_EXCEEDED` | Rule 4 |

If the lab's tools are missing or say unauthorized, the user signs in once: `/mcp`, the server
`plugin:qupai-lab:lab`, Authenticate. That opens QuPai's own sign-in in the browser.
