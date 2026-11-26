# qupai-lab-plugin

- Read README.md and docs/boundary.md before changing this repository.
- This is a public Claude Code plugin: never put a credential, a deployment's private address or a customer's name in it.
  The QuPai addresses a user needs come from the plugin's settings (`userConfig` in `.claude-plugin/plugin.json`).
- Check before every commit: `claude plugin validate .` must pass.
- Name only the tools the lab's MCP server has (InstruCore's: `machines`, `describe`, `read`, `write`, `sweep`,
  `measure`, `measure_status`, `measure_stop` (0.42.0), `start_session`, `renew_session`, `end_session`,
  `start_simulated_session`, `reconnect`; `stop` went in InstruCore 0.15.0). When InstruCore's tools or roles change, change the `lab-operator` skill with them.
- Raise `version` in `.claude-plugin/plugin.json` for every change a user should pick up.
- `sdk/python` is the Lab SDK (`qupai-lab`, its own version in `qupai_lab/__init__.py`): standard library only.
  It calls InstruCore's REST routes; when their shapes change, change it with them. Run its tests
  (`sdk/python/README.md`, Tests) before every commit that touches it. Publishing to PyPI needs the owner's word.
- Use English for documentation and identifiers. Stage explicit files.
