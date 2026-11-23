# Boundary

## Ownership

The Claude Code plugin for a QuPai lab (QuPai labs redesign, LR12): the lab's MCP server entry, the `lab-operator`
skill and the `watch` skill. It runs no service and holds no data or secret. The tools are InstruCore's, on the lab PC,
reached through QuPai's gateway at `<gateway>/mcp/lab-<lab id>`; QuPai's sign-in (OAuth) and the member's role on the
lab's benches decide what a call may do. The live page is QuPai's web app (`/lab/bench/lab-<lab id>`, with
`?machine=<bench id>` for one bench).
The Lab SDK (`sdk/python`, the Python package `qupai-lab`) is a library for scripts: it calls InstruCore's REST
routes, through QuPai's sandbox door with a lab project's key or at the lab PC with a local key, and holds no
secret of its own.

## Release boundary

This repository owns its own release and license (MIT). Users install it from GitHub through its marketplace
(`.claude-plugin/marketplace.json`); the QuPai platform pins a tested revision as a component and does not make this
repository inherit the platform's license.

## Compatibility

The skills name InstruCore's MCP tools and QuPai's lab roles (view, operate and configure on a bench; admin on the
lab). The live page's address follows
QuPai's web routes. A change to either is a change here, with a new `version`.
