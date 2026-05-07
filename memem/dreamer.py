"""Async offline consolidation pass — memem v2 m4.

The 'dreamer' is the slow strong-model pass that runs offline (cron, idle,
manual). Per the closed-loop memory research (Letta sleep-time, Cognee
memify, A-MEM evolution), this is the dominant 2025-2026 architectural
pattern: cheap online model + strong offline consolidator.

Hard safety rules:
  1. NEVER touches L0 memories (catastrophic-forgetting + poisoning defense)
  2. NEVER touches decay_immune-flagged memories
  3. Always produces reviewable diff at MEMEM_DIR/dreams/<ts>.json
  4. Default mode is dry-run; --apply required to mutate
  5. Diff log preserved on apply for rollback

Three consolidation categories:
  - demotion_candidates: L2 memories with should_demote=3 + low attribution
  - contradiction_pairs: bi-temporal candidates from m1
  - cluster_summarization: dense L2 clusters per project

References:
  - Letta sleep-time compute
  - Cognee memify (https://docs.cognee.ai/core-concepts/main-operations/memify)
  - A-MEM (arXiv 2502.12110)
  - A-MemGuard (arXiv 2510.02373) — diff log + rollback for poisoning defense
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from memem.models import DEFAULT_LAYER, LAYER_L0, MEMEM_DIR

log = logging.getLogger("memem-dreamer")

DREAMS_DIR = MEMEM_DIR / "dreams"
LOW_ATTRIBUTION_THRESHOLD = 0.2
CLUSTER_SIMILARITY_THRESHOLD = 0.7
CLUSTER_MIN_SIZE = 5
SONNET_MODEL = "claude-sonnet-4-7"  # strong offline model


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_protected(memory: dict) -> bool:
    """Hard safety check — never modify L0 or decay_immune memories."""
    layer = memory.get("layer")
    if layer is None:
        layer = DEFAULT_LAYER
    if int(layer) == LAYER_L0:
        return True
    return bool(memory.get("decay_immune", False))


def _recent_attribution(memory_id: str, sample_size: int = 20) -> float | None:
    """Mean aggregate attribution score over the most recent N events for this memory.
    Returns None if no events yet."""
    from memem.models import MEMEM_DIR
    events_path = MEMEM_DIR / "events.jsonl"
    if not events_path.exists():
        return None
    samples = []
    try:
        with open(events_path) as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # telemetry._log_event writes the event-name field as 'op'
                if event.get("op") != "slice_attribution":
                    continue
                data = event
                if data.get("memory_id") != memory_id:
                    continue
                agg = data.get("aggregate")
                if agg is not None:
                    samples.append(float(agg))
    except OSError:
        return None
    if not samples:
        return None
    recent = samples[-sample_size:]
    return sum(recent) / len(recent)


def find_demotion_candidates(memories: list[dict]) -> list[dict]:
    """Memories that m3.should_demote suggests demoting AND have low recent attribution."""
    from memem.decay import should_demote

    candidates = []
    for mem in memories:
        if _is_protected(mem):
            continue
        suggested = should_demote(mem)
        if suggested is None:
            continue
        attr = _recent_attribution(mem.get("id", ""))
        if attr is not None and attr >= LOW_ATTRIBUTION_THRESHOLD:
            continue  # still being used → don't demote
        candidates.append({
            "memory_id": mem.get("id"),
            "title": mem.get("title", "")[:80],
            "current_layer": int(mem.get("layer", 2) or 2),
            "suggested_layer": suggested,
            "recent_attribution": attr,
            "reason": f"low strength + attribution={attr}",
        })
    return candidates


def find_contradiction_pairs(memories: list[dict]) -> list[dict]:
    """Bi-temporal pairs needing canonicalization.

    Surface candidates where:
      - Memory A's `contradicts: [B]` AND both A.invalid_at and B.invalid_at are None
      - i.e., we have an unresolved contradiction
    """
    pairs = []
    by_id = {m.get("id"): m for m in memories if m.get("id")}
    for mem in memories:
        if _is_protected(mem):
            continue
        if mem.get("invalid_at"):
            continue  # already resolved
        contradicts = mem.get("contradicts") or []
        for other_id in contradicts:
            other = by_id.get(other_id)
            if not other or _is_protected(other) or other.get("invalid_at"):
                continue
            pairs.append({
                "memory_a": mem.get("id"),
                "memory_a_title": mem.get("title", "")[:80],
                "memory_b": other_id,
                "memory_b_title": other.get("title", "")[:80],
                "memory_a_created": mem.get("created_at"),
                "memory_b_created": other.get("created_at"),
            })
    return pairs


def find_cluster_summaries(memories: list[dict]) -> list[dict]:
    """Detect clusters of similar memories. Returns proposal dicts (dry-run).

    Each proposal:
        {
            "project": str,
            "cluster_ids": list[str],
            "pattern_title": str,
            "pattern_content": str,
            "similarity_mean": float,
        }

    Steps:
    1. Group by project, skip L0 + decay_immune (anchored).
    2. For each project group >= CLUSTER_MIN_SIZE: embed content, build cosine
       similarity matrix, greedy cluster.
    3. For each qualifying cluster: compute similarity_mean, call Sonnet to
       synthesize a pattern. Skip clusters on Sonnet failure.

    Idempotent and side-effect free — does NOT write to vault.
    Returns [] if embedding is unavailable.
    """
    try:
        from memem.embedding_index import _embed_text
    except ImportError:
        log.info("embedding unavailable; skipping cluster summarization")
        return []

    try:
        import numpy as np
    except ImportError:
        log.info("numpy unavailable; skipping cluster summarization")
        return []

    # Group non-protected memories by project
    by_project: dict[str, list[dict]] = {}
    for mem in memories:
        if _is_protected(mem):
            continue
        project = mem.get("project") or "general"
        by_project.setdefault(project, []).append(mem)

    proposals: list[dict] = []

    for project, group in by_project.items():
        if len(group) < CLUSTER_MIN_SIZE:
            continue

        # Embed each memory's content
        embeddings: list[list[float]] = []
        valid_group: list[dict] = []
        for mem in group:
            content = mem.get("essence") or mem.get("content") or mem.get("title") or ""
            vec = _embed_text(content)
            if vec is None:
                continue
            embeddings.append(vec)
            valid_group.append(mem)

        if len(valid_group) < CLUSTER_MIN_SIZE:
            continue

        # Build L2-normalized float32 matrix
        mat = np.array(embeddings, dtype=np.float32)
        # Rows should already be L2-normalized from _embed_text (normalize_embeddings=True)
        # but normalize again for safety
        norms = np.linalg.norm(mat, axis=1, keepdims=True).astype(np.float32)
        norms = np.where(norms == 0, np.float32(1.0), norms)
        mat = (mat / norms).astype(np.float32)

        # Pairwise cosine similarity
        sim_matrix = mat @ mat.T  # shape (N, N)
        n = len(valid_group)

        # Greedy clustering
        tried = [False] * n
        for i in range(n):
            if tried[i]:
                continue
            tried[i] = True  # mark as tried regardless of outcome
            # Find all other un-tried memories with high similarity to i
            similar_indices = [i]
            for j in range(n):
                if j == i or tried[j]:
                    continue
                if float(sim_matrix[i, j]) >= CLUSTER_SIMILARITY_THRESHOLD:
                    similar_indices.append(j)

            if len(similar_indices) < CLUSTER_MIN_SIZE:
                continue

            # Form cluster — mark all members as tried
            for j in similar_indices:
                tried[j] = True

            cluster_mems = [valid_group[k] for k in similar_indices]
            cluster_ids = [m.get("id", "") for m in cluster_mems]

            # Compute mean pairwise similarity (excluding diagonal)
            idx = np.array(similar_indices)
            sub_sim = sim_matrix[np.ix_(idx, idx)]
            mask = 1 - np.eye(len(idx))
            if mask.sum() > 0:
                similarity_mean = float((sub_sim * mask).sum() / mask.sum())
            else:
                similarity_mean = 1.0

            # Generate synthesis via Sonnet
            n_cluster = len(cluster_mems)
            mem_lines = []
            for k, m in enumerate(cluster_mems, 1):
                title = m.get("title", "")[:80]
                content = (m.get("essence") or m.get("content") or "")[:200]
                mem_lines.append(f"[{k}] {title} — {content}")
            mem_block = "\n".join(mem_lines)

            prompt = (
                f"The following {n_cluster} memories share high pairwise semantic similarity"
                f" ({similarity_mean:.2f}).\n"
                f"Write ONE concise pattern that captures the recurring theme."
                f" Output in this exact format:\n"
                f"TITLE: <one-line title, <80 chars>\n"
                f"---\n"
                f"<pattern body, 2-4 sentences>\n\n"
                f"Memories:\n{mem_block}\n"
            )

            try:
                result = subprocess.run(
                    ["claude", "-p", "--model", SONNET_MODEL],
                    input=prompt,
                    capture_output=True, text=True, timeout=60,
                    start_new_session=True,  # signal isolation; matches contradiction subprocess
                )
                if result.returncode != 0:
                    log.warning(
                        "Sonnet cluster synthesis failed (rc=%d) for project %s",
                        result.returncode, project,
                    )
                    continue

                out = result.stdout.strip()
                # Parse TITLE: ... \n--- \n <body>
                title_line = ""
                body_text = ""
                if "TITLE:" in out:
                    after_title = out[out.index("TITLE:") + len("TITLE:"):].strip()
                    if "---" in after_title:
                        parts = after_title.split("---", 1)
                        title_line = parts[0].strip()
                        body_text = parts[1].strip()
                    else:
                        lines = after_title.splitlines()
                        title_line = lines[0].strip()
                        body_text = "\n".join(lines[1:]).strip()
                else:
                    log.warning(
                        "Sonnet response for project %s missing TITLE: marker; skipping cluster",
                        project,
                    )
                    continue

                if not title_line or not body_text:
                    log.warning(
                        "Sonnet response for project %s missing title or body; skipping cluster",
                        project,
                    )
                    continue

                proposals.append({
                    "project": project,
                    "cluster_ids": cluster_ids,
                    "pattern_title": title_line[:80],
                    "pattern_content": body_text,
                    "similarity_mean": similarity_mean,
                })

            except subprocess.TimeoutExpired:
                log.warning("Sonnet synthesis timed out for cluster in project %s; skipping", project)
                continue
            except Exception as exc:
                log.warning("Sonnet synthesis error for project %s: %s; skipping", project, exc)
                continue

    return proposals


def build_diff(memories: list[dict]) -> dict:
    """Build the full proposed-changes diff for a vault snapshot."""
    return {
        "generated_at": _now_iso(),
        "vault_size": len(memories),
        "demotion_candidates": find_demotion_candidates(memories),
        "contradiction_pairs": find_contradiction_pairs(memories),
        "cluster_summaries": find_cluster_summaries(memories),
        "stats": {
            "l0_count": sum(1 for m in memories if int(m.get("layer", 2) or 2) == 0),
            "decay_immune_count": sum(1 for m in memories if m.get("decay_immune")),
        },
    }


def _judge_contradiction_with_sonnet(pair: dict) -> dict | None:
    """Ask Sonnet which memory in a contradiction pair is canonical.

    Returns:
        None on any failure
        {'winner': memory_id, 'loser': memory_id, 'reasoning': str} on success
    """
    prompt = (
        f"Two memories from a developer's vault contradict each other.\n"
        f"Decide which is canonical (more recent, more specific, or more accurate).\n\n"
        f"Memory A (id={pair['memory_a']}, created={pair['memory_a_created']}):\n"
        f"  {pair['memory_a_title']}\n\n"
        f"Memory B (id={pair['memory_b']}, created={pair['memory_b_created']}):\n"
        f"  {pair['memory_b_title']}\n\n"
        f"Output ONLY a JSON object: "
        f'{{"winner_id": "<id>", "loser_id": "<id>", "reasoning": "<one sentence>"}}'
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", SONNET_MODEL, "--tools", ""],
            input=prompt,
            capture_output=True, text=True, timeout=60,
            start_new_session=True,
        )
        if result.returncode != 0:
            return None
        out = result.stdout.strip()
        # Find JSON object
        start = out.find("{")
        end = out.rfind("}")
        if start < 0 or end <= start:
            return None
        parsed = json.loads(out[start:end + 1])
        winner = parsed.get("winner_id")
        loser = parsed.get("loser_id")
        if winner not in (pair["memory_a"], pair["memory_b"]):
            return None
        if loser not in (pair["memory_a"], pair["memory_b"]):
            return None
        return {
            "winner": winner,
            "loser": loser,
            "reasoning": parsed.get("reasoning", "")[:200],
        }
    except Exception as exc:
        log.warning("Sonnet contradiction judgment failed: %s", exc)
        return None


def apply_diff(diff: dict, dry_run: bool = True) -> dict:
    """Apply demotion + contradiction-resolution + cluster summary proposals.

    dry_run=True (default): no mutations; returns counts only.
    dry_run=False: writes layer changes via _write_obsidian_memory; calls
    invalidate_memory for contradiction losers; creates cluster-summary
    pattern memories and marks constituents with clustered_into.

    Returns: {'demoted': N, 'invalidated': M, 'clustered': K, 'errors': [...]}.
    """
    from memem.obsidian_store import _find_memory, _write_obsidian_memory, invalidate_memory

    demoted = 0
    invalidated = 0
    clustered = 0
    errors: list[str] = []

    for c in diff.get("demotion_candidates", []):
        mem_id = c.get("memory_id")
        new_layer = c.get("suggested_layer")
        if not mem_id or new_layer is None:
            continue
        if dry_run:
            demoted += 1
            continue
        mem = _find_memory(mem_id)
        if not mem:
            errors.append(f"demote: not found {mem_id}")
            continue
        if _is_protected(mem):
            errors.append(f"demote: protected {mem_id}")
            continue
        mem["layer"] = int(new_layer)
        try:
            _write_obsidian_memory(mem)
            demoted += 1
        except Exception as exc:
            errors.append(f"demote {mem_id}: {exc}")

    for p in diff.get("contradiction_pairs", []):
        if dry_run:
            # Don't actually call Sonnet in dry-run — just count what we'd ask
            invalidated += 1
            continue
        decision = _judge_contradiction_with_sonnet(p)
        if not decision:
            errors.append(f"contradiction: judgment failed {p['memory_a']} vs {p['memory_b']}")
            continue
        try:
            invalidate_memory(decision["loser"], replaced_by=decision["winner"])
            invalidated += 1
        except Exception as exc:
            errors.append(f"invalidate {decision['loser']}: {exc}")

    for proposal in diff.get("cluster_summaries", []):
        if dry_run:
            continue
        # Create new pattern memory with layer=2
        try:
            from memem.obsidian_store import _make_memory
            pattern_mem = _make_memory(
                content=proposal["pattern_content"],
                title=proposal["pattern_title"],
                tags=["cluster-summary", "dreamer-synthesis"],
                project=proposal.get("project", "general"),
                layer=2,
            )
            _write_obsidian_memory(pattern_mem)
            new_memory_id = pattern_mem["id"]
        except Exception as exc:
            errors.append(f"cluster pattern write failed for project {proposal.get('project')}: {exc}")
            continue

        # Mark each constituent with clustered_into
        for constituent_id in proposal.get("cluster_ids", []):
            try:
                constituent = _find_memory(constituent_id)
                if not constituent:
                    errors.append(f"cluster constituent not found: {constituent_id}")
                    continue
                constituent["clustered_into"] = new_memory_id
                _write_obsidian_memory(constituent)
            except Exception as exc:
                errors.append(f"cluster constituent update {constituent_id}: {exc}")

        clustered += 1

    return {"demoted": demoted, "invalidated": invalidated, "clustered": clustered, "errors": errors}


def write_diff_log(diff: dict) -> Path:
    """Persist the diff to ~/.memem/dreams/<ts>.json. Returns the path."""
    DREAMS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = DREAMS_DIR / f"{ts}.json"
    path.write_text(json.dumps(diff, indent=2, default=str))
    return path


def run_dream_cycle(dry_run: bool = True) -> dict:
    """End-to-end: load vault, build diff, write log, optionally apply.

    Returns: {'diff_path': str, 'diff': dict, 'apply_result': dict | None}.
    """
    from memem.obsidian_store import _obsidian_memories

    memories = _obsidian_memories(include_deprecated=False)
    diff = build_diff(memories)
    diff_path = write_diff_log(diff)
    apply_result = apply_diff(diff, dry_run=dry_run)
    return {
        "diff_path": str(diff_path),
        "diff": diff,
        "apply_result": apply_result,
        "dry_run": dry_run,
    }
