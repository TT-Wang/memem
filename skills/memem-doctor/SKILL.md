---
name: memem-doctor
description: Run a preflight health check on memem — Python version, mcp dep, claude CLI, vault writability, capabilities. Use when the user reports memem misbehaving, has just installed the plugin, or asks "why isn't this working".
allowed-tools: [Bash]
---

Run the portable bootstrap doctor. This is the same launcher Codex and Claude Code use for MCP and hooks:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --doctor
```

Display the full output verbatim. If the result says **FAILING**, the user must fix the listed blockers before memem will work — walk them through each one:

- **mcp package missing** → the runtime venv is stale; run `bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --doctor` again to let it resync, or inspect `~/.memem/bootstrap.log`
- **~/.memem not writable** → set `MEMEM_DIR=/writable/path` env var in their shell profile
- **vault not writable** → set `MEMEM_OBSIDIAN_VAULT=/writable/path` env var

If the result says **DEGRADED**, the system works but Haiku-powered context assembly and smart-recall are disabled because the `claude` CLI isn't on PATH. memem falls back to FTS-only recall, which is still useful — just less smart. Tell the user how to fix it (install Claude Code CLI, ensure it's on PATH) but don't block them on it.

If the result says **HEALTHY**, tell the user everything looks good and suggest running `/memem-status` to see memory counts, or just continue their conversation — memem MCP and hooks are available.
