"""Quarantine handler for malformed Obsidian memory files.

Extracted from obsidian_store.py (F-002a) to isolate frontmatter error handling.
"""

import hashlib
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("memem-obsidian")


# Module-level constant — read once at import. Tests that need a different mode
# should set MEMEM_FRONTMATTER_STRICT before importing (or reload this module).
_FRONTMATTER_STRICT_MODE = os.environ.get("MEMEM_FRONTMATTER_STRICT", "quarantine").lower()


def _handle_malformed_frontmatter(md_file: Path, reason: str) -> None:
    """Dispatch a malformed-frontmatter file per MEMEM_FRONTMATTER_STRICT.

    Modes:
      - skip       : log warning, leave the file in place
      - quarantine : move to ~/.memem/quarantine/<original_relpath> (default)
      - raise      : ValueError

    The quarantine destination preserves the relative path under the vault root
    so multiple files with the same name from different scopes don't collide.
    The MEMEM_DIR path is read dynamically (from models) so that test fixtures
    that override MEMEM_DIR are honoured at call time.
    """
    # Read mode dynamically so env-var overrides in tests take effect.
    mode = os.environ.get("MEMEM_FRONTMATTER_STRICT", "quarantine").lower()

    if mode == "raise":
        raise ValueError(f"Malformed frontmatter in {md_file}: {reason}")

    if mode == "quarantine":
        from memem.models import MEMEM_DIR  # dynamic — picks up test-fixture patches
        try:
            quarantine_root = MEMEM_DIR / "quarantine"
            # Preserve the file's name + a short hash of its source path so two
            # files named "memory.md" from different folders don't clash.
            src_hash = hashlib.sha1(  # noqa: S324
                str(md_file).encode(), usedforsecurity=False
            ).hexdigest()[:8]
            dest = quarantine_root / f"{src_hash}_{md_file.name}"
            quarantine_root.mkdir(parents=True, exist_ok=True)
            try:
                md_file.rename(dest)
            except OSError:
                # Fall back to copy+unlink for cross-fs moves
                shutil.copy2(md_file, dest)
                md_file.unlink(missing_ok=True)
            log.warning(
                "Quarantined malformed memory file %s → %s (%s)",
                md_file, dest, reason,
            )
            return
        except Exception as exc:  # noqa: BLE001 — quarantine must never crash the reader
            log.warning(
                "Quarantine failed for %s (%s); falling back to skip: %s",
                md_file, reason, exc,
            )
            # Fall through to skip

    # Default / skip
    log.warning("Skipping malformed memory file %s (%s)", md_file, reason)
