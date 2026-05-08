"""Vault registry for cross-vault memory recall.

Reads ~/.memem/vaults.json to discover registered memory vaults.
When vaults.json does not exist, falls back to the single default vault
(MEMEM_OBSIDIAN_VAULT / 'memem' / 'memories') so existing single-vault
setups continue to work without any configuration.

JSON shape of vaults.json:
    [{"id": "default", "path": "/abs/path/to/memories"}, ...]
"""

import json

import structlog

log = structlog.get_logger("memem-vault-registry")


def load_vault_registry() -> list[dict]:
    """Load the vault registry from ~/.memem/vaults.json.

    Returns a list of dicts, each with at least ``id`` and ``path`` keys.

    When vaults.json does not exist, returns a synthetic single-element list
    pointing at the current default vault so callers work without configuration.
    Invalid entries (missing ``id`` or ``path``) are skipped with a warning.
    """
    from memem.models import MEMEM_DIR

    vaults_json = MEMEM_DIR / "vaults.json"

    if not vaults_json.exists():
        # Determine the default memories path the same way models.py does:
        # OBSIDIAN_VAULT / 'memem' / 'memories' (with cortex fallback logic
        # handled by the imported constant).
        from memem.models import OBSIDIAN_MEMORIES_DIR
        return [{"id": "default", "path": str(OBSIDIAN_MEMORIES_DIR)}]

    try:
        raw = json.loads(vaults_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read vaults.json, using default vault", error=str(exc))
        from memem.models import OBSIDIAN_MEMORIES_DIR
        return [{"id": "default", "path": str(OBSIDIAN_MEMORIES_DIR)}]

    if not isinstance(raw, list):
        log.warning("vaults.json must be a JSON array, using default vault")
        from memem.models import OBSIDIAN_MEMORIES_DIR
        return [{"id": "default", "path": str(OBSIDIAN_MEMORIES_DIR)}]

    valid: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("Skipping invalid vault entry (not a dict)", entry=repr(entry))
            continue
        vault_id = entry.get("id")
        vault_path = entry.get("path")
        if not vault_id:
            log.warning("Skipping vault entry missing 'id'", entry=repr(entry))
            continue
        if not vault_path:
            log.warning("Skipping vault entry missing 'path'", entry=repr(entry), vault_id=vault_id)
            continue
        valid.append({"id": str(vault_id), "path": str(vault_path)})

    if not valid:
        log.warning("vaults.json had no valid entries, using default vault")
        from memem.models import OBSIDIAN_MEMORIES_DIR
        return [{"id": "default", "path": str(OBSIDIAN_MEMORIES_DIR)}]

    return valid
