# Contributing to Cortex

Thanks for your interest in contributing!

## Dev setup

```bash
git clone https://github.com/TT-Wang/cortex-plugin.git
cd cortex-plugin
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Running tests

```bash
pytest                          # run all tests
pytest -v                       # verbose
pytest --cov=cortex-mcp-server  # with coverage
pytest tests/test_security.py   # single file
```

## Code style

- `ruff` for linting and formatting
- `mypy` for type checking
- Pre-commit hooks run on every commit — install them

```bash
ruff check .
ruff format .
mypy cortex-mcp-server
```

## PR process

1. Fork the repo
2. Create a branch: `git checkout -b feat/my-feature`
3. Make changes, add tests
4. Run `ruff check .` and `pytest`
5. Commit with a clear message
6. Push and open a PR against `master`

## Commit conventions

We use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation only
- `refactor:` code change that neither fixes a bug nor adds a feature
- `test:` adding or updating tests
- `chore:` maintenance

Example: `feat: add schema_version field to memory frontmatter`

## Architecture notes

Cortex has three layers:

1. **Storage** — Obsidian markdown (source of truth) + SQLite FTS5 (search index)
2. **Mining** — Background daemon extracts knowledge from session JSONL files
3. **Assembly** — Haiku synthesizes query-tailored briefings from memories

Key modules:
- `models.py` — data types and constants
- `security.py` — threat scanning (prompt injection, credential exfil)
- `telemetry.py` — access tracking, event log
- `search_index.py` — SQLite FTS5
- `obsidian_store.py` — memory I/O, dedup, scoring
- `playbook.py` — grow and refine
- `assembly.py` — context assembly

Do NOT touch:
- `hooks/` unless you understand the Claude Code hook protocol
- Security scanning patterns (coordinate with maintainers)

## Questions?

Open an issue or start a discussion on GitHub.
