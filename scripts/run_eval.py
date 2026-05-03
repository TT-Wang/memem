#!/usr/bin/env python3
"""Run the memem evaluation set + canary verification.

Usage:
    python3 scripts/run_eval.py                # run all cases + canaries
    python3 scripts/run_eval.py --canary-only  # canary trip-wire only (fast)
    python3 scripts/run_eval.py --plant-canaries  # one-shot: plant canaries
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
    print("=== Canary verification ===")
    print(json.dumps(canary_result, indent=2))

    if args.canary_only:
        sys.exit(0 if canary_result["pass"] else 1)

    from memem.eval.eval_set import run_all_cases
    eval_result = run_all_cases()
    print("\n=== Eval cases ===")
    print(json.dumps({
        "total": eval_result["total_cases"],
        "passed": eval_result["passed"],
        "failed": eval_result["failed"],
        "by_ability": eval_result["by_ability"],
    }, indent=2))

    overall_pass = canary_result["pass"] and eval_result["failed"] == 0
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
