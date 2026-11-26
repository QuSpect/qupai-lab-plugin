---
name: lab-operator
description: Operate a QuPai lab's instruments through this plugin's lab tools (machines, describe, read, write, sweep, sessions). Use when the user asks to look at, measure with, rehearse on or control the instruments of their QuPai lab.
---

# Operating a QuPai lab

This plugin's `lab` server is the user's QuPai lab: QuPai's gateway in front of the lab PC's InstruCore, the program
that drives the lab's instruments. Every call is checked there: the user's role on the bench, the lab's limits, and
who holds each machine.

One lab PC may serve several benches of the lab. Each bench is a real machine of its own, with its own instruments,
holder and queue; no instrument is shared between benches. `machines` lists each by its id (a lab with only one
bench calls it `bench`; never assume that name). The user holds a role on each bench, and a bench they hold no role
on is not listed and answers `NOT_FOUND`. What the role on a bench allows there:

| Role | Simulated copies | The real machine |
| --- | --- | --- |
| View | Everything | Read, watch live |
| Operate | Everything | Also hold it in a session and write to it |
| Configure | Everything | Also end someone else's session there; building the bench and its limits are on the lab's page, not tools |
| Admin (the lab's) | Everything | Configure on every bench |

The lab's admin decides how far an agent outside QuPai goes: at view, you hold the view role here whatever the
user's own role is.

## Rules

1. **Look first.** Call `machines`, then `describe` the machine or instrument before acting: its operations, units
   and limits. When the lab has several benches, work on the one the user names, by its id; if they did not say
   which, ask. Never carry a value, a session or a conclusion from one bench to another.
2. **Rehearse on a simulated copy.** `start_simulated_session` with the machine's pool gives a fresh copy with the same
   instruments, operations and limits and no hardware. End it with `end_session` when done.
3. **Ask before a real write.** Before anything that changes a real machine, tell the user exactly what you will
   write, and wait for their yes. Writes need a session: `start_session` (it may put you in a queue). Renew it only
   while working, and end it when done.
4. **Limits hold for everyone.** You get the lab's stricter agent limits. On `LIMIT_EXCEEDED`, report the limit and
   the value; never nudge a value to get past a limit.
5. **Stop when something looks wrong.** Make no further change and tell the user what you saw. There is no
   machine-wide stop: an instrument is turned off by its own operations (an output off, a sequencer's `stop`), written
   in your session like any other write, after telling the user exactly what you will write. Without a session, name
   the instrument and leave it to the user.
6. **Say where each number came from:** the real machine, or a simulated copy.
7. **The user can watch.** `/qupai-lab:watch` opens the lab's live page in QuPai (`/qupai-lab:watch <bench>` one
   bench), where every call shows in the activity strip, yours marked as an agent's.

## Scripts

For a scan or any loop of many calls, a Python script with the Lab SDK (`pip install qupai-lab`) works the same lab
through InstruCore's REST API, under the same roles, limits and sessions. It is not only for QuPai projects: outside
QuPai, `Lab.from_qupai("<lab id>")` finds the lab with the user's own `qupai` sign-in (`qupai login`), for a lab QuPai
staff granted them or the lab workstation they sit at (there, through the workstation's door). Rehearse the script
on a simulated copy first when the lab has one. `with lab.machine("<bench>") as m:` holds the machine only for the
block and ends the session when the block ends, also when it raises; tell the user before running it on a real
machine, as for any real write. In a QuPai lab project a live run's card lists what the rehearsal wrote
(`changes.json`); on a lab with no simulated copy nothing can be rehearsed, so declare the run's writes with
`declare_changes(experiment, [...])` (role, op, fixed, and quantity/min/max or ranges) and commit them: the card shows
them as declared, not rehearsed, and the lab refuses any other write in the run. The SDK's README
(`sdk/python/README.md` in this plugin's repository) has the rest.

**A live run holds its bench as its own session.** A session whose purpose is `run <id>` is a live run a member
approved in QuPai, running on that bench now: not someone's forgotten session, and not a run still waiting. Never end
it (`end_session` on it, with configure) and never tell the user to release it from the bench page: that stops the
run, which then fails. Wait for the bench, or tell the user they can cancel the run in QuPai.

## When a call is refused

| Error | What it means |
| --- | --- |
| `SESSION_REQUIRED`, `BENCH_NOT_HELD` | Start a session on that machine first |
| `BENCH_HELD`, `MACHINE_IN_SESSION` | Someone else holds it; reads still work. Offer to join the queue. A holder's purpose `run <id>` is a live run running there: never end it |
| `QUEUE_WAITING`, `SESSION_PENDING` | You are in the queue; it says where |
| `FORBIDDEN` with `required` | The user's role on that bench, or the lab's setting for outside agents, does not allow it. Tell the user |
| `NOT_FOUND` for a machine | No such bench, or one the user holds no role on. Check `machines` |
| `LIMIT_EXCEEDED` | Rule 4 |

If the lab's tools are missing or say unauthorized, the user signs in once: `/mcp`, the server
`plugin:qupai-lab:lab`, Authenticate. That opens QuPai's own sign-in in the browser. If they are still refused once
signed in, the user may not work this lab directly from outside QuPai: that is a grant only QuPai staff make. Then
the lab's agent is the way (`qupai ask -lab <lab> "…"`): it does the work in QuPai, with its own approvals.
