#!/usr/bin/env python3
"""LLM-judge backfill of memory layers via Haiku.

Phase B-LLM. The heuristic backfill (scripts/backfill_layers.py) only does
mild L2->L1 elevation because classify_layer() requires importance>=4 for L0
and importance<=2 + content<200 chars + no related[] for L3 — almost no
existing memory satisfies either. This script asks Haiku to classify each
memory, which can recognize L0 (project-identity statements) and L3 (rare
archival) by content rather than by importance score.

Costs at ~50 memories per Haiku batch:
  ~57 batches for 2813 memories
  ~$0.05-0.10 total at Haiku pricing (input $0.25/M, output $1.25/M)
  ~10-15 minutes wall time depending on Haiku throughput

Usage:
    python3 scripts/backfill_layers_llm.py --pilot                # 50 memories, dry-run
    python3 scripts/backfill_layers_llm.py --pilot --apply        # 50 memories, write
    python3 scripts/backfill_layers_llm.py --apply                # full run, write
    python3 scripts/backfill_layers_llm.py --apply --resume       # skip already-classified

Idempotency: with --resume, memories whose layer is non-default (anything
other than the original L2) are assumed already classified and skipped.
Without --resume, every memory is re-judged.

Output:
  .forge/state/memem-layer-llm-pilot.json  (pilot mode)
  .forge/state/memem-layer-llm-full.json   (full mode)
  .forge/state/memem-layer-llm-trace.jsonl (every Haiku decision, append-only)
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memem.models import DEFAULT_LAYER  # noqa: E402
from memem.obsidian_store import (  # noqa: E402
    _obsidian_memories,
    _save_memory,
)


BATCH_SIZE = 25
HAIKU_TIMEOUT_SECONDS = 180
MAX_PARALLEL_BATCHES = 5


SYSTEM_PROMPT = """You are classifying memory entries into layers L0/L1/L2/L3 for a code-agent memory system.

Rules:
  L0 — Project identity. Structural facts about a SPECIFIC project that should always
       be loaded when working on that project. Examples: "my-app uses Next.js + Postgres",
       "auth is JWT RS256", "tests run via pnpm vitest", "deploys to Cloudflare Workers".
       Cap: each project should have at most ~20 L0 memories. Reserve L0 for the most
       structural facts only.

  L1 — Generic conventions. Cross-project patterns that apply broadly. Examples:
       "always use exponential backoff with jitter", "prefer pytest fixtures over
       setUp/tearDown", "small focused PRs over big bundles". Should NOT be project-
       specific.

  L2 — Domain-specific (DEFAULT). Project-relevant lessons, decisions, or facts that
       don't rise to identity-level but matter for THIS project. Most memories live here.

  L3 — Rare/archival. Niche failure modes, one-off incidents, or context that's only
       relevant when a specific issue resurfaces. Examples: "fix for issue #1247
       (specific bcrypt async gotcha)", "weird interaction between feature flags X+Y
       in 2024-Q3". L3 memories are NOT auto-recalled — only fetched explicitly.

Input: a JSON array of memories with id, title, content, project, importance, tags.
Output: a JSON array of {id, layer, reason} objects, where layer is 0/1/2/3 and
reason is <=15 words explaining the classification.

CRITICAL: output ONLY valid JSON. No prose before or after. No markdown code fences.
Every input memory must appear in the output exactly once.

If you're unsure between two layers, prefer L2 (the safe default).
"""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


def _build_input_payload(memories: list[dict]) -> str:
    """Compact JSON input for Haiku — title + truncated content + scalar fields."""
    items = []
    for m in memories:
        items.append({
            "id": m.get("id", "")[:8],
            "title": _truncate(m.get("title", "") or "", 120),
            "content": _truncate(m.get("essence", "") or "", 400),
            "project": m.get("project", "general"),
            "importance": m.get("importance", 3),
            "tags": (m.get("domain_tags") or m.get("tags") or [])[:6],
        })
    return json.dumps(items, ensure_ascii=False)


def _parse_haiku_response(raw: str) -> list[dict]:
    """Extract JSON array from Haiku output, tolerating wrapping."""
    raw = raw.strip()
    # Strip code fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    # Find first [ ... last ] to handle pre/post prose
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)


def _judge_batch(memories: list[dict]) -> list[dict]:
    """One Haiku call. Returns list of {id, layer, reason} for this batch."""
    payload = _build_input_payload(memories)
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "",
             "--system-prompt", SYSTEM_PROMPT],
            input=payload,
            capture_output=True,
            text=True,
            timeout=HAIKU_TIMEOUT_SECONDS,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Haiku call timed out after {HAIKU_TIMEOUT_SECONDS}s")
    if result.returncode != 0:
        raise RuntimeError(
            f"Haiku call failed (exit {result.returncode}): {result.stderr[:500]}"
        )
    parsed = _parse_haiku_response(result.stdout)
    if not isinstance(parsed, list):
        raise RuntimeError(f"Expected JSON array, got {type(parsed).__name__}")
    return parsed


def _validate_judgements(memories: list[dict], judgements: list[dict]) -> dict[str, int]:
    """Map memory_id -> layer; reject invalid layers, missing ids."""
    mem_ids_short = {m.get("id", "")[:8]: m.get("id", "") for m in memories}
    layer_map: dict[str, int] = {}
    for j in judgements:
        short_id = str(j.get("id", ""))
        if short_id not in mem_ids_short:
            continue
        full_id = mem_ids_short[short_id]
        layer = j.get("layer")
        if not isinstance(layer, int) or layer < 0 or layer > 3:
            continue
        layer_map[full_id] = layer
    return layer_map


def _format_distribution(counter: Counter, total: int) -> str:
    lines = []
    for layer in sorted(counter):
        count = counter[layer]
        pct = 100.0 * count / total if total else 0.0
        lines.append(f"  L{layer}: {count:5d} ({pct:5.1f}%)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true",
                        help="Only classify first 50 memories; report distribution + samples")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip memories whose layer differs from DEFAULT_LAYER (assume already classified)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"Memories per Haiku call (default: {BATCH_SIZE})")
    parser.add_argument("--parallel", type=int, default=MAX_PARALLEL_BATCHES,
                        help=f"Concurrent Haiku batches (default: {MAX_PARALLEL_BATCHES}; set 1 for sequential)")
    args = parser.parse_args()

    print("Loading vault…")
    all_mems = _obsidian_memories(include_deprecated=False)
    print(f"Loaded {len(all_mems)} memories.")

    # Filter
    if args.resume:
        before = len(all_mems)
        all_mems = [m for m in all_mems if int(m.get("layer", DEFAULT_LAYER)) == DEFAULT_LAYER]
        print(f"--resume: skipping {before - len(all_mems)} already-classified memories.")

    if args.pilot:
        all_mems = all_mems[:50]
        print(f"--pilot: classifying first {len(all_mems)} memories.")

    if not all_mems:
        print("No memories to classify.")
        sys.exit(0)

    state_dir = Path(__file__).resolve().parent.parent / ".forge" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    trace_path = state_dir / "memem-layer-llm-trace.jsonl"

    print(f"\nClassifying in batches of {args.batch_size} ({args.parallel} concurrent)…")
    t0 = time.time()

    layer_map: dict[str, int] = {}
    judgement_records: list[dict] = []  # for trace
    failed_batches = 0
    completed = 0

    batches = [all_mems[i:i + args.batch_size] for i in range(0, len(all_mems), args.batch_size)]
    total_batches = len(batches)

    def _run_batch(idx_batch: tuple[int, list[dict]]) -> tuple[int, dict, list[dict], Exception | None]:
        idx, batch = idx_batch
        try:
            judgements = _judge_batch(batch)
            batch_map = _validate_judgements(batch, judgements)
            records = [
                {"batch": idx, "id": j.get("id"), "layer": j.get("layer"),
                 "reason": j.get("reason", "")[:200]}
                for j in judgements
            ]
            return (idx, batch_map, records, None)
        except Exception as exc:
            return (idx, {}, [], exc)

    if args.parallel <= 1:
        # Sequential path (kept for debugging / rate-limit fallback)
        for i, batch in enumerate(batches, 1):
            print(f"  Batch {i}/{total_batches} ({len(batch)} memories)…", flush=True)
            idx, batch_map, records, err = _run_batch((i, batch))
            if err:
                failed_batches += 1
                print(f"    -> FAILED: {err}")
            else:
                layer_map.update(batch_map)
                judgement_records.extend(records)
                completed += 1
                print(f"    -> {len(batch_map)}/{len(batch)} classified")
    else:
        # Parallel path
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            futures = {ex.submit(_run_batch, (i, b)): i for i, b in enumerate(batches, 1)}
            for fut in as_completed(futures):
                idx, batch_map, records, err = fut.result()
                if err:
                    failed_batches += 1
                    print(f"  Batch {idx}/{total_batches} FAILED: {err}", flush=True)
                else:
                    layer_map.update(batch_map)
                    judgement_records.extend(records)
                    completed += 1
                    elapsed = time.time() - t0
                    rate = completed / elapsed if elapsed > 0 else 0
                    eta_min = (total_batches - completed) / rate / 60 if rate > 0 else 0
                    print(
                        f"  Batch {idx}/{total_batches} ✓ {len(batch_map)} classified "
                        f"[{completed}/{total_batches} done, ~{eta_min:.1f}min ETA]",
                        flush=True,
                    )

    elapsed = time.time() - t0
    print(f"\nElapsed: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"Classified: {len(layer_map)}/{len(all_mems)}")
    print(f"Failed batches: {failed_batches}")

    # Write trace
    with trace_path.open("a") as f:
        for record in judgement_records:
            f.write(json.dumps(record) + "\n")

    # Distribution
    counter = Counter(layer_map.values())
    print(f"\nLayer distribution (n={len(layer_map)}):")
    print(_format_distribution(counter, len(layer_map)))

    # Per-project L0
    l0_per_project = Counter()
    for m in all_mems:
        if layer_map.get(m.get("id"), DEFAULT_LAYER) == 0:
            l0_per_project[m.get("project", "general")] += 1
    if l0_per_project:
        print(f"\nL0 per project (top 10):")
        for project, count in l0_per_project.most_common(10):
            print(f"  {project}: {count}")

    # Sample classifications
    print(f"\nSample classifications (3 per layer):")
    samples_by_layer: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    by_id = {m.get("id"): m for m in all_mems}
    for record in judgement_records:
        full_id = next((mid for mid in by_id if mid.startswith(str(record["id"]))), None)
        if not full_id:
            continue
        mem = by_id[full_id]
        layer = record.get("layer")
        if layer in samples_by_layer and len(samples_by_layer[layer]) < 3:
            title = (mem.get("title") or "")[:60]
            reason = record.get("reason", "")[:80]
            samples_by_layer[layer].append(f"  L{layer}  {title}  ({reason})")
    for layer in sorted(samples_by_layer):
        print(f"  L{layer}:")
        for sample in samples_by_layer[layer]:
            print(f"  {sample}")

    # Save report
    suffix = "pilot" if args.pilot else "full"
    report_path = state_dir / f"memem-layer-llm-{suffix}.json"
    report_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "pilot" if args.pilot else "full",
        "applied": args.apply,
        "total_memories": len(all_mems),
        "classified": len(layer_map),
        "failed_batches": failed_batches,
        "elapsed_seconds": round(elapsed, 1),
        "distribution": dict(counter),
        "l0_per_project": dict(l0_per_project),
    }
    report_path.write_text(json.dumps(report_data, indent=2))
    print(f"\nReport: {report_path}")
    print(f"Trace: {trace_path}")

    if not args.apply:
        print("\nDRY-RUN. No files changed. Re-run with --apply to write.")
        sys.exit(0)

    # Apply
    print("\nApplying changes…")
    applied = 0
    skipped = 0
    failed = 0
    for m in all_mems:
        new_layer = layer_map.get(m.get("id"))
        if new_layer is None:
            skipped += 1
            continue
        old_layer = int(m.get("layer", DEFAULT_LAYER))
        if new_layer == old_layer:
            skipped += 1
            continue
        m["layer"] = new_layer
        try:
            _save_memory(m)
            applied += 1
        except Exception as exc:
            failed += 1
            print(f"  failed {m.get('id', '?')[:8]}: {exc}")
    print(f"Applied: {applied} / Skipped: {skipped} / Failed: {failed}")


if __name__ == "__main__":
    main()
