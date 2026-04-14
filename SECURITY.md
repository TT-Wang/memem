# Security Policy

## Supported versions

The latest minor release on `master` is the only actively supported version.
Older versions do not receive security fixes.

## Reporting a vulnerability

Please report security issues **privately** via GitHub Security Advisories:

> https://github.com/TT-Wang/memem/security/advisories/new

Do **not** open a public issue for security reports — doing so may expose
other memem users before a fix is available.

When reporting, include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a minimal proof-of-concept
- The affected memem version (check `python3 -m memem.server --status`)
- Your environment (OS, Python version, Obsidian vault layout if relevant)

I will acknowledge the report within 72 hours and aim to publish a fix and
advisory within 14 days of validation, depending on severity.

## Scope

In scope for security reports:

- Prompt injection or credential exfiltration past memem's content scanner
- Path traversal in `memory_import` or any file-reading code path
- Shell injection in any subprocess call (Haiku invocations, miner wrapper, bootstrap shim)
- Unauthorized memory disclosure across project scopes
- Vault-write failures that silently lose data
- Bootstrap shim privilege escalation or arbitrary-code-execution via `uv`

Out of scope (not a security issue):

- memem failing gracefully when `claude` CLI is missing (this is documented degraded mode)
- memem reading files under `$HOME` that the running user can already read
- Missing `memory_delete` / `memory_update` tools (a feature gap, not a vulnerability)

## Defence-in-depth features already in place

- Every memory write runs through `scan_memory_content()` (see `memem/security.py`)
  which rejects prompt-injection patterns, credential exfiltration signatures,
  invisible-unicode smuggling, and shell-metacharacter payloads.
- `memory_import` refuses paths outside `$HOME` via `Path.is_relative_to()`.
- All memory, session state, and telemetry writes are atomic (`tmp + fsync + os.replace`)
  and guarded by `fcntl.flock` during read-modify-write operations.
- The bootstrap shim canary-tests directory writability before exec'ing the server.
- The MCP server is stdio-only — no network ports are opened.
- No credentials, API keys, or tokens are stored by memem itself. The `claude` CLI
  subprocess uses the user's existing Claude Code session.

Thanks for helping keep memem users safe.
