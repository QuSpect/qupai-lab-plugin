# qupai-lab-plugin

- Read README.md and docs/boundary.md before changing this repository.
- This is a public Claude Code plugin: never put a credential, a deployment's private address or a customer's name in it.
  The QuPai addresses a user needs come from the plugin's settings (`userConfig` in `.claude-plugin/plugin.json`).
- Check before every commit: `claude plugin validate .` must pass.
- Name only the tools the lab's MCP server has (InstruCore's: `machines`, `describe`, `read`, `write`, `sweep`,
  `start_session`, `renew_session`, `end_session`, `start_simulated_session`, `reconnect`, `stop`). When InstruCore's
  tools or roles change, change the `lab-operator` skill with them.
- Raise `version` in `.claude-plugin/plugin.json` for every change a user should pick up.
- Use English for documentation and identifiers. Stage explicit files.
