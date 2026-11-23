---
name: watch
description: Open this QuPai lab's live page (Instruments, live) in the user's browser.
disable-model-invocation: true
---

# Watch the lab

The lab's live page in QuPai is:

${user_config.qupai_web}/lab/bench/lab-${user_config.lab}

When the user names a bench (`/qupai-lab:watch <bench>`, the bench's id as the `machines` tool lists it), add
`?machine=<bench>` to open that bench; without one the page opens the lab's first bench and offers the others.

Open it in the user's browser with the command for their system, and show them the address too, in case it does
not open:

- macOS: `open "<address>"`
- Linux: `xdg-open "<address>"`
- Windows: `start "" "<address>"`

The page needs their QuPai sign-in in that browser. Its controls follow their role on the bench, and its activity strip
shows every call, theirs and their agents'.
