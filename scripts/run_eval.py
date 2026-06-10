#!/usr/bin/env python3
"""Run the memem evaluation set + canary verification.

Eval cases run against an ISOLATED tmp vault so the planted memories
aren't outranked by the live vault's content. Canary verification runs
against the LIVE vault (that's the whole point of canaries).

Usage:
    python3 scripts/run_eval.py                # canary check + isolated eval cases
    python3 scripts/run_eval.py --canary-only  # canary trip-wire only (fast)
    python3 scripts/run_eval.py --plant-canaries  # one-shot: plant canaries into live vault
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run_eval_in_tmp_vault() -> dict:
    """Run the eval cases against an isolated tmp vault + tmp state dir.

    Reloads memem modules so they pick up the env-var paths instead of
    the live vault. Restores env + reloads back to live config on exit.
    """
    saved_vault = os.environ.get("MEMEM_OBSIDIAN_VAULT")
    saved_dir = os.environ.get("MEMEM_DIR")

    with tempfile.TemporaryDirectory(prefix="memem-eval-") as tmp_root:
        tmp_root_path = Path(tmp_root)
        tmp_vault = tmp_root_path / "vault"
        tmp_dir = tmp_root_path / "state"
        (tmp_vault / "memem" / "memories").mkdir(parents=True)
        (tmp_vault / "memem" / "playbooks").mkdir(parents=True)
        tmp_dir.mkdir()

        os.environ["MEMEM_OBSIDIAN_VAULT"] = str(tmp_vault)
        os.environ["MEMEM_DIR"] = str(tmp_dir)
        os.environ["MEMEM_EVAL_VAULT_OVERRIDE"] = "1"

        # Reload modules so the path constants pick up the new env values
        # (these were captured at first import against the live vault).
        from memem import models, obsidian_store, search_index, telemetry
        importlib.reload(models)
        importlib.reload(telemetry)
        importlib.reload(search_index)
        importlib.reload(obsidian_store)
        # Force eval_set to re-import its dependencies through the reloaded modules
        from memem.eval import eval_set
        importlib.reload(eval_set)

        try:
            return eval_set.run_all_cases()
        finally:
            # Restore env + re-reload back to live config so the rest of the
            # script (canary verification) sees the real vault.
            os.environ.pop("MEMEM_EVAL_VAULT_OVERRIDE", None)
            if saved_vault is not None:
                os.environ["MEMEM_OBSIDIAN_VAULT"] = saved_vault
            else:
                os.environ.pop("MEMEM_OBSIDIAN_VAULT", None)
            if saved_dir is not None:
                os.environ["MEMEM_DIR"] = saved_dir
            else:
                os.environ.pop("MEMEM_DIR", None)
            importlib.reload(models)
            importlib.reload(telemetry)
            importlib.reload(search_index)
            importlib.reload(obsidian_store)
            importlib.reload(eval_set)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canary-only", action="store_true",
                        help="skip eval cases; verify canaries only")
    parser.add_argument("--plant-canaries", action="store_true",
                        help="one-shot: plant canaries into vault then exit")
    args = parser.parse_args()

    from memem.eval.canaries import plant_canaries, verify_canaries_intact

    if args.plant_canaries:
        planted = plant_canaries()
        print(f"Planted {len(planted)} canaries.")
        return

    canary_result = verify_canaries_intact()
    print("=== Canary verification (live vault) ===")
    print(json.dumps(canary_result, indent=2))

    if args.canary_only:
        sys.exit(0 if canary_result["pass"] else 1)

    eval_result = _run_eval_in_tmp_vault()
    print("\n=== Eval cases (isolated tmp vault) ===")
    print(json.dumps({
        "total": eval_result["total_cases"],
        "passed": eval_result["passed"],
        "failed": eval_result["failed"],
        "by_ability": eval_result["by_ability"],
    }, indent=2))
    if eval_result["failed"]:
        print("\n--- Failures ---")
        for r in eval_result["results"]:
            if not r["pass"]:
                print(json.dumps(r, indent=2, default=str))

    overall_pass = canary_result["pass"] and eval_result["failed"] == 0
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
