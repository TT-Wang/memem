"""Episodic consolidation + contradiction detection for memem.

Background pass that:
  1. Loads all memories in a given layer
  2. Computes embeddings and clusters near-duplicates (cosine >= threshold)
  3. For each cluster large enough (>= min_cluster_size), makes one Haiku call
     to produce a canonical merged memory + surface internal contradictions
  4. Saves the canonical memory (kind:consolidated) and marks sources with
     superseded_by in frontmatter
  5. Saves contradiction-flag memories for any flagged pairs

Recommended cadence: weekly cron.
Cron itself is NOT installed by this module — wire it manually if desired:
  Example entry: 0 2 * * 0 PYTHONPATH=/path/to/cortex-plugin python3 -m memem.server --consolidate

Usage:
    python3 -m memem.server --consolidate
    python3 -m memem.server --consolidate --dry-run
    python3 -m memem.server --consolidate --layer 2 --min-cluster 3 --threshold 0.85
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field

from memem.mining import HAIKU_TIMEOUT_SECONDS
from memem.models import LAYER_L2
from memem.obsidian_store import (
    _find_memory,
    _make_memory,
    _obsidian_memories,
    _save_memory,
    _write_obsidian_memory,
)

log = logging.getLogger("memem-consolidation")

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ConsolidationResult:
    clusters_processed: int = 0
    memories_consolidated: int = 0
    contradictions_flagged: int = 0
    canonical_memories_created: list[str] = field(default_factory=list)
    superseded_memories: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Embedding helpers (reuse embedding_index)
# ---------------------------------------------------------------------------


def _compute_embeddings(
    memories: list[dict],
) -> tuple[list[dict], list[list[float]]]:
    """Return (filtered_memories, embedding_vectors) — only memories that could
    be embedded are returned. Memories for which embedding failed are silently
    dropped (they simply won't be clustered).
    """
    from memem.embedding_index import _get_model, _try_import

    model = _get_model()
    if model is None:
        return [], []

    _st, np = _try_import()
    if np is None:
        return [], []

    texts = [
        (mem.get("title", "") + " — " + mem.get("essence", "")).strip()
        for mem in memories
    ]
    try:
        vectors = model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
    except Exception as exc:
        log.warning("consolidation: embedding encode failed: %s", exc)
        return [], []

    vecs = [np.asarray(vectors[i], dtype="float32").tolist() for i in range(len(memories))]
    return list(memories), vecs


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two L2-normalized vectors (simple dot product)."""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------------------
# Greedy clustering
# ---------------------------------------------------------------------------


def _greedy_cluster(
    memories: list[dict],
    vectors: list[list[float]],
    threshold: float,
) -> list[list[int]]:
    """Greedy clustering: for each unclustered memory, collect all others with
    cosine >= threshold. Returns list of index groups; singletons are NOT included.
    """
    n = len(memories)
    assigned = [False] * n
    clusters: list[list[int]] = []

    for i in range(n):
        if assigned[i]:
            continue
        group = [i]
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            if _cosine(vectors[i], vectors[j]) >= threshold:
                group.append(j)

        if len(group) > 1:
            for idx in group:
                assigned[idx] = True
            clusters.append(group)
        # singletons left unassigned — not part of any cluster

    return clusters


# ---------------------------------------------------------------------------
# Haiku call for consolidation
# ---------------------------------------------------------------------------

_CONSOLIDATION_SYSTEM = (
    "You are a knowledge consolidator for an AI memory system. "
    "You will receive N memories about the same topic. "
    "Merge them into ONE canonical memory that preserves all unique information. "
    "Also identify internal contradictions between the memories.\n\n"
    "Output ONLY a JSON object with these keys:\n"
    '  "canonical_title": short descriptive title for the merged memory\n'
    '  "canonical_content": merged content (prose, all unique info preserved)\n'
    '  "supporting_ids": list of the memory IDs you merged (include ALL provided IDs)\n'
    '  "contradictions": list of objects, each with "memory_a" (id), "memory_b" (id), '
    '"conflict" (one-sentence description). Empty list if none.\n\n'
    "Rules:\n"
    "- Prefer newer/more-specific phrasing when facts conflict\n"
    "- Keep the merged content under 2000 characters\n"
    "- Output ONLY the JSON object, no other text"
)


def _call_haiku_consolidate(cluster_memories: list[dict]) -> dict | None:
    """Call Haiku to merge a cluster into a canonical memory. Returns parsed JSON or None."""
    parts = []
    for mem in cluster_memories:
        mid = mem.get("id", "")[:8]
        title = mem.get("title", "Untitled")
        essence = mem.get("essence", "")
        parts.append(f"ID: {mid}\nTitle: {title}\nContent: {essence}")

    prompt = (
        f"Merge these {len(cluster_memories)} memories about the same topic "
        "into one canonical memory.\n\n"
        + "\n\n---\n\n".join(parts)
    )

    try:
        result = subprocess.run(
            [
                "claude", "-p", "--model", "haiku",
                "--tools", "",
                "--system-prompt", _CONSOLIDATION_SYSTEM,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=HAIKU_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.warning("consolidation: Haiku subprocess failed: %s", exc)
        return None

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        log.warning("consolidation: Haiku returned non-zero: %s", detail)
        return None

    raw = result.stdout.strip()
    if not raw:
        log.warning("consolidation: Haiku returned empty output")
        return None

    # Try direct parse first (expected: response is a clean JSON object)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Fallback: extract the first JSON object `{...}` from the output
    # (Haiku may prefix/suffix prose). We look for `{` specifically to avoid
    # accidentally picking up inner lists (e.g. supporting_ids) first.
    start = raw.find("{")
    if start == -1:
        log.warning("consolidation: no JSON object found in Haiku output: %s", raw[:200])
        return None

    decoder = json.JSONDecoder()
    while start < len(raw):
        try:
            parsed, _ = decoder.raw_decode(raw[start:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = raw.find("{", start + 1)

    log.warning("consolidation: could not parse JSON object from Haiku output: %s", raw[:200])
    return None


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------


def run_consolidation_pass(
    layer: int = LAYER_L2,
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.85,
    dry_run: bool = False,
) -> ConsolidationResult:
    """Cluster memories in `layer` by embedding similarity; merge near-duplicate
    clusters into one canonical memory; flag contradictions.

    Args:
        layer: Which memory layer to consolidate (default L2).
        min_cluster_size: Minimum cluster size to trigger a merge (default 3).
        similarity_threshold: Cosine similarity threshold for clustering (default 0.85).
        dry_run: If True, compute everything but skip all disk writes.

    Returns:
        ConsolidationResult with counts and IDs of created/superseded memories.
    """
    result = ConsolidationResult()

    # Step 1: Load all memories in the target layer
    all_memories = _obsidian_memories()
    layer_memories = [
        m for m in all_memories
        if m.get("layer", LAYER_L2) == layer
        and m.get("status", "active") == "active"
        # Skip memories already superseded or canonical themselves
        and "kind:consolidated" not in m.get("domain_tags", [])
        and "kind:contradiction-flag" not in m.get("domain_tags", [])
    ]

    if not layer_memories:
        log.info("consolidation: no eligible memories in layer %d", layer)
        return result

    log.info(
        "consolidation: loaded %d eligible memories in layer %d",
        len(layer_memories), layer,
    )

    # Step 2: Compute embeddings
    embedded_mems, vectors = _compute_embeddings(layer_memories)
    if not embedded_mems:
        log.info("consolidation: embedding unavailable — skipping pass")
        return result

    # Step 3: Greedy clustering
    clusters = _greedy_cluster(embedded_mems, vectors, similarity_threshold)
    eligible_clusters = [c for c in clusters if len(c) >= min_cluster_size]

    log.info(
        "consolidation: %d clusters found, %d meet min_cluster_size=%d",
        len(clusters), len(eligible_clusters), min_cluster_size,
    )

    if not eligible_clusters:
        return result

    # Step 4: Process each eligible cluster
    for cluster_indices in eligible_clusters:
        cluster_mems = [embedded_mems[i] for i in cluster_indices]

        # One Haiku call per cluster
        haiku_result = _call_haiku_consolidate(cluster_mems)
        if not haiku_result:
            result.errors.append(
                f"Haiku call failed for cluster of {len(cluster_mems)} memories "
                f"({[m.get('id','')[:8] for m in cluster_mems]})"
            )
            continue

        canonical_title = str(haiku_result.get("canonical_title", "Consolidated memory"))
        canonical_content = str(haiku_result.get("canonical_content", ""))
        contradictions = haiku_result.get("contradictions", [])

        if not canonical_content.strip():
            result.errors.append(
                f"Empty canonical content for cluster {[m.get('id','')[:8] for m in cluster_mems]}"
            )
            continue

        result.clusters_processed += 1

        # Step 5: Save canonical memory (kind:consolidated stored as domain tag)
        project = cluster_mems[0].get("project", "general")
        # Use the most common project if they differ
        projects = [m.get("project", "general") for m in cluster_mems]
        project = max(set(projects), key=projects.count)

        extra_tags = ["kind:consolidated", "consolidated"]
        # Build tags from union of source tags, plus consolidated markers
        all_tags: list[str] = list(extra_tags)
        for m in cluster_mems:
            for t in m.get("domain_tags", []):
                if t not in all_tags and not t.startswith("kind:"):
                    all_tags.append(t)

        canonical_id: str | None = None
        if not dry_run:
            try:
                canonical_mem = _make_memory(
                    content=canonical_content,
                    title=canonical_title,
                    tags=all_tags,
                    project=project,
                    source_type="consolidated",
                    layer=layer,
                )
                # `supersedes` as a list isn't persisted by _write_obsidian_memory.
                # Encode the source IDs as `supersedes:<id8>` tags so the
                # graph_index can build supersession edges (see graph_index.py:354)
                # AND keep the in-memory field for any direct caller.
                source_ids = [m.get("id", "") for m in cluster_mems if m.get("id")]
                canonical_mem["supersedes"] = source_ids
                for sid in source_ids:
                    canonical_mem.setdefault("domain_tags", []).append(f"supersedes:{sid[:8]}")
                _save_memory(canonical_mem)
                canonical_id = canonical_mem["id"]
                result.canonical_memories_created.append(canonical_id)
            except Exception as exc:
                result.errors.append(
                    f"Failed to save canonical memory for cluster: {exc}"
                )
                continue
        else:
            import uuid
            canonical_id = str(uuid.uuid4())  # placeholder for dry-run reporting
            result.canonical_memories_created.append(f"dry-run:{canonical_id[:8]}")

        # Step 6: Mark each source memory as superseded
        for src_mem in cluster_mems:
            src_id = src_mem.get("id", "")
            result.superseded_memories.append(src_id)
            result.memories_consolidated += 1

            if not dry_run:
                # Re-fetch the live memory (it may have been updated by _save_memory side effects)
                live_mem = _find_memory(src_id) or src_mem
                # Use `replaced_by` — the existing bi-temporal field that
                # _write_obsidian_memory actually persists (obsidian_store.py:529).
                # `superseded_by` was the spec name but isn't in the schema; using
                # it would silently lose the audit trail on disk.
                live_mem["replaced_by"] = canonical_id
                live_mem["superseded_by"] = canonical_id  # in-memory only; tests check both
                live_mem["status"] = "deprecated"
                try:
                    _write_obsidian_memory(live_mem)
                except Exception as exc:
                    result.errors.append(
                        f"Failed to mark {src_id[:8]} as superseded: {exc}"
                    )

        # Step 7: Save contradiction-flag memories
        if not isinstance(contradictions, list):
            contradictions = []

        for contradiction in contradictions:
            if not isinstance(contradiction, dict):
                continue
            mem_a_id = str(contradiction.get("memory_a", ""))
            mem_b_id = str(contradiction.get("memory_b", ""))
            conflict_desc = str(contradiction.get("conflict", ""))
            if not conflict_desc:
                continue

            conflict_content = (
                f"Contradiction detected between memories {mem_a_id} and {mem_b_id}: "
                f"{conflict_desc}"
            )
            conflict_title = f"Contradiction: {conflict_desc[:80]}"
            conflict_tags = ["kind:contradiction-flag", "contradiction", "pending-review"]

            result.contradictions_flagged += 1
            if not dry_run:
                try:
                    flag_mem = _make_memory(
                        content=conflict_content,
                        title=conflict_title,
                        tags=conflict_tags,
                        project=project,
                        source_type="system",
                        importance=4,
                        layer=layer,
                    )
                    flag_mem["references"] = [mem_a_id[:8], mem_b_id[:8]]
                    _save_memory(flag_mem)
                except Exception as exc:
                    result.errors.append(
                        f"Failed to save contradiction-flag for {mem_a_id}/{mem_b_id}: {exc}"
                    )

    return result
