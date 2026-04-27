#!/usr/bin/env python3
"""Conservative vault dedup pass.

Auto-deprecates memory clusters at >= 0.97 cosine similarity (embedding-based).
Writes pairs in the 0.85-0.97 band to a review file for manual inspection.
Does NOT touch anything in the 0.85-0.97 band.

Usage:
    python3 scripts/vault_dedup.py              # live run
    python3 scripts/vault_dedup.py --dry-run    # simulate, no mutations

Outputs:
    .forge/state/memem-vault-cleanup-dedup-audit.jsonl
    .forge/state/memem-vault-cleanup-dedup-review.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vault-dedup")

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / ".forge" / "state"
AUDIT_PATH = STATE_DIR / "memem-vault-cleanup-dedup-audit.jsonl"
REVIEW_PATH = STATE_DIR / "memem-vault-cleanup-dedup-review.json"

THRESH_AUTO = 0.97      # >= this: auto-deprecate duplicates
THRESH_REVIEW = 0.85    # >= this and < THRESH_AUTO: surface for manual review


# ---------------------------------------------------------------------------
# Union-Find for clustering
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])  # path compression
        return self._parent[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def clusters(self, members: list[str]) -> dict[str, list[str]]:
        """Return root -> [members] mapping."""
        result: dict[str, list[str]] = {}
        for m in members:
            r = self.find(m)
            result.setdefault(r, []).append(m)
        return result


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def _bigram_jaccard(a: str, b: str) -> float:
    """Bigram Jaccard similarity — fallback when embedding is unavailable."""
    def bigrams(s: str) -> set[tuple[str, str]]:
        tokens = s.lower().split()
        return set(zip(tokens, tokens[1:], strict=False)) if len(tokens) >= 2 else set()

    ba, bb = bigrams(a), bigrams(b)
    if not ba and not bb:
        return 0.0
    union = ba | bb
    return len(ba & bb) / len(union) if union else 0.0


def _memory_text(mem: dict) -> str:
    return (mem.get("title", "") + " " + mem.get("essence", "")).strip()


# ---------------------------------------------------------------------------
# Keeper selection
# ---------------------------------------------------------------------------

def _keeper(mems: list[dict]) -> dict:
    """Given a cluster of memories, return the one to keep.

    Priority: highest importance, then newest created_at.
    """
    def sort_key(m: dict) -> tuple:
        importance = m.get("importance") or 0
        created_at = m.get("created_at") or ""
        return (importance, created_at)

    return max(mems, key=sort_key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> None:
    sys.path.insert(0, str(REPO_ROOT))

    from memem.obsidian_store import _deprecate_memory, _find_memory, _obsidian_memories

    # ------------------------------------------------------------------
    # 1. Load active memories
    # ------------------------------------------------------------------
    log.info("Loading active memories...")
    mems = _obsidian_memories(include_deprecated=False)
    log.info("Loaded %d active memories", len(mems))

    mem_by_id = {m["id"]: m for m in mems}
    all_ids = list(mem_by_id.keys())
    n = len(all_ids)

    # ------------------------------------------------------------------
    # 2. Build embedding vectors — with fallback to bigram Jaccard
    # ------------------------------------------------------------------
    use_embeddings = False
    vectors = None   # numpy array (n, 384) or None
    FALLBACK_WARNING = False

    try:
        import numpy as np

        from memem.embedding_index import _get_model, _load_index, is_available

        if is_available():
            _load_index()
            from memem.embedding_index import _index_ids, _index_matrix

            # Build a full-coverage matrix by encoding any missing memories
            model = _get_model()

            if model is not None and _index_matrix is not None and len(_index_ids) > 0:
                log.info("Embedding index loaded: %d entries", len(_index_ids))

                # Map index IDs to their row positions
                index_row: dict[str, int] = {mid: i for i, mid in enumerate(_index_ids)}

                # Resolve each active memory's row (short or full ID)
                covered_ids = []
                covered_rows = []
                missing_ids = []

                for mem_id in all_ids:
                    if mem_id in index_row:
                        covered_ids.append(mem_id)
                        covered_rows.append(index_row[mem_id])
                    else:
                        # Try prefix match (short IDs in index)
                        found_row = None
                        for idx_id, row in index_row.items():
                            if len(idx_id) == 8 and mem_id.startswith(idx_id) or len(mem_id) == 8 and idx_id.startswith(mem_id):
                                found_row = row
                                break
                        if found_row is not None:
                            covered_ids.append(mem_id)
                            covered_rows.append(found_row)
                        else:
                            missing_ids.append(mem_id)

                log.info(
                    "%d memories in embedding index, %d need fresh encoding",
                    len(covered_ids), len(missing_ids),
                )

                # Fetch pre-built vectors for covered memories
                covered_matrix = _index_matrix[covered_rows, :]  # shape (M, 384)

                # Encode the missing memories
                if missing_ids:
                    missing_texts = [
                        _memory_text(mem_by_id[mid]) for mid in missing_ids
                    ]
                    log.info("Encoding %d new memories...", len(missing_ids))
                    fresh_vecs = model.encode(
                        missing_texts,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                        batch_size=64,
                    )
                    fresh_matrix = np.asarray(fresh_vecs, dtype=np.float32)
                    # Stack: covered first, then missing
                    full_matrix = np.vstack([covered_matrix, fresh_matrix])
                    ordered_ids = covered_ids + missing_ids
                else:
                    full_matrix = covered_matrix
                    ordered_ids = covered_ids

                # Reorder to match all_ids order
                row_map = {mid: i for i, mid in enumerate(ordered_ids)}
                reorder = [row_map[mid] for mid in all_ids]
                vectors = full_matrix[reorder, :]  # (n, 384)
                use_embeddings = True
                log.info("Embedding matrix ready: shape %s", vectors.shape)

    except Exception as exc:
        log.warning("Embedding index unavailable (%s), falling back to bigram Jaccard", exc)
        FALLBACK_WARNING = True

    if not use_embeddings:
        log.warning(
            "FALLBACK: Using bigram Jaccard similarity (no sentence-transformers). "
            "This is a coarse approximation — consider installing the embedding extra."
        )
        FALLBACK_WARNING = True

    # ------------------------------------------------------------------
    # 3. Pairwise similarity — collect >=0.85 pairs
    # ------------------------------------------------------------------
    log.info("Computing pairwise similarities for %d memories (%d pairs)...", n, n * (n - 1) // 2)

    auto_pairs: list[tuple[str, str, float]] = []    # >= 0.97
    review_pairs: list[tuple[str, str, float]] = []  # 0.85 <= x < 0.97

    if use_embeddings and vectors is not None:
        import numpy as np
        # Batch compute: split into chunks to avoid OOM on large n
        CHUNK = 500
        for i in range(0, n, CHUNK):
            chunk_vecs = vectors[i:i + CHUNK, :]          # (chunk, 384)
            # dot product with remaining memories (j > i to avoid duplicates)
            sims_matrix = chunk_vecs @ vectors.T           # (chunk, n)
            chunk_size = chunk_vecs.shape[0]
            for ci in range(chunk_size):
                global_i = i + ci
                for j in range(global_i + 1, n):
                    sim = float(sims_matrix[ci, j])
                    if sim >= THRESH_AUTO:
                        auto_pairs.append((all_ids[global_i], all_ids[j], sim))
                    elif sim >= THRESH_REVIEW:
                        review_pairs.append((all_ids[global_i], all_ids[j], sim))
    else:
        # Fallback: bigram Jaccard pairwise (slower)
        texts = [_memory_text(mem_by_id[mid]) for mid in all_ids]
        total = n * (n - 1) // 2
        done = 0
        log_interval = max(1, total // 20)
        for i in range(n):
            for j in range(i + 1, n):
                sim = _bigram_jaccard(texts[i], texts[j])
                if sim >= THRESH_AUTO:
                    auto_pairs.append((all_ids[i], all_ids[j], sim))
                elif sim >= THRESH_REVIEW:
                    review_pairs.append((all_ids[i], all_ids[j], sim))
                done += 1
                if done % log_interval == 0:
                    log.info("  %.0f%% done (%d/%d pairs)", 100 * done / total, done, total)

    log.info(
        "Similarity scan done: %d auto-dedup pairs (>=%.2f), %d review pairs (%.2f-%.2f)",
        len(auto_pairs), THRESH_AUTO, len(review_pairs), THRESH_REVIEW, THRESH_AUTO,
    )

    # ------------------------------------------------------------------
    # 4. Cluster via union-find on >=0.97 pairs
    # ------------------------------------------------------------------
    uf = UnionFind()
    for a, b, _sim in auto_pairs:
        uf.union(a, b)

    clusters = uf.clusters(all_ids)
    # Only keep multi-member clusters (singleton == no duplicate)
    dup_clusters = {root: mids for root, mids in clusters.items() if len(mids) > 1}
    log.info("Formed %d duplicate clusters at >= %.2f threshold", len(dup_clusters), THRESH_AUTO)

    # ------------------------------------------------------------------
    # 5. Build review clusters for 0.85-0.97 band (union-find separately)
    # ------------------------------------------------------------------
    uf_review = UnionFind()
    for a, b, _sim in review_pairs:
        uf_review.union(a, b)

    review_clusters_raw = uf_review.clusters([p[0] for p in review_pairs] + [p[1] for p in review_pairs])
    review_dup_clusters = {root: mids for root, mids in review_clusters_raw.items() if len(mids) > 1}

    # ------------------------------------------------------------------
    # 6. Execute auto-deprecation for >=0.97 clusters
    # ------------------------------------------------------------------
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()

    audit_records: list[dict] = []
    clusters_deprecated = 0
    mems_deprecated = 0

    with AUDIT_PATH.open("a", encoding="utf-8") as audit_f:
        for cluster_idx, (_root, member_ids) in enumerate(dup_clusters.items()):
            member_mems = [mem_by_id[mid] for mid in member_ids if mid in mem_by_id]
            if not member_mems:
                continue

            keeper = _keeper(member_mems)
            keeper_id = keeper["id"]
            to_deprecate = [m for m in member_mems if m["id"] != keeper_id]

            # Collect similarity info for audit
            pair_sims = {
                f"{a}->{b}": round(sim, 4)
                for a, b, sim in auto_pairs
                if a in member_ids and b in member_ids
            }

            for dep_mem in to_deprecate:
                dep_id = dep_mem["id"]
                reason = f"duplicate_of_{keeper_id}"

                success = False
                if not dry_run:
                    try:
                        result = _deprecate_memory(dep_id, reason=reason)
                        success = bool(result)
                    except Exception as exc:
                        # _deprecate_memory may raise after writing the vault file
                        # (e.g. if _remove_index_line fails on a root-owned _index.md).
                        # Verify success by checking the actual vault file status.
                        log.warning(
                            "Exception during deprecate %s: %s — verifying vault file",
                            dep_id[:12], exc,
                        )
                        try:
                            from memem.obsidian_store import _find_memory
                            check = _find_memory(dep_id)
                            success = check is not None and check.get("status") == "deprecated"
                            if success:
                                log.info(
                                    "Vault file for %s IS deprecated despite exception "
                                    "(index update failed — root-owned _index.md)",
                                    dep_id[:12],
                                )
                        except Exception as check_exc:
                            log.warning("Could not verify status of %s: %s", dep_id[:12], check_exc)
                            success = False
                else:
                    success = True  # simulate success in dry-run

                record = {
                    "cluster_id": f"auto_{cluster_idx:04d}",
                    "action": "auto_deprecated" if not dry_run else "dry_run_would_deprecate",
                    "keeper_id": keeper_id,
                    "keeper_title": keeper.get("title", ""),
                    "deprecated_id": dep_id,
                    "deprecated_title": dep_mem.get("title", ""),
                    "reason": reason,
                    "similarities": pair_sims,
                    "all_cluster_ids": member_ids,
                    "success": success,
                    "timestamp": now,
                    "similarity_method": "cosine_embedding" if use_embeddings else "bigram_jaccard_fallback",
                }
                audit_records.append(record)
                audit_f.write(json.dumps(record) + "\n")
                audit_f.flush()

                if success:
                    mems_deprecated += 1

            clusters_deprecated += 1

    # Write single-cluster keeper audit records for clusters that had no deprecation needed
    # (all singletons get implicit "kept" but we only need to note multi-member outcomes)

    # ------------------------------------------------------------------
    # 7. Write review JSON (0.85-0.97 band, DO NOT TOUCH)
    # ------------------------------------------------------------------
    review_cluster_list = []
    for cluster_idx, (_root, member_ids) in enumerate(review_dup_clusters.items()):
        member_mems = [mem_by_id.get(mid) for mid in member_ids]
        member_mems = [m for m in member_mems if m is not None]

        # Collect relevant pair similarities
        pair_sims = {}
        for a, b, sim in review_pairs:
            if a in member_ids and b in member_ids:
                pair_sims[f"{a}->{b}"] = round(sim, 4)

        review_cluster_list.append({
            "cluster_id": f"review_{cluster_idx:04d}",
            "band": f"{THRESH_REVIEW}-{THRESH_AUTO}",
            "member_count": len(member_ids),
            "members": [
                {
                    "id": m["id"],
                    "title": m.get("title", ""),
                    "project": m.get("project", ""),
                    "importance": m.get("importance", 3),
                    "created_at": m.get("created_at", ""),
                    "essence_snippet": (m.get("essence", "") or "")[:120],
                }
                for m in member_mems
            ],
            "pair_similarities": pair_sims,
        })

    review_output = {
        "generated_at": now,
        "threshold_auto": THRESH_AUTO,
        "threshold_review": THRESH_REVIEW,
        "total_review_pairs": len(review_pairs),
        "cluster_count": len(review_cluster_list),
        "similarity_method": "cosine_embedding" if use_embeddings else "bigram_jaccard_fallback",
        "note": "DO NOT AUTO-DEPRECATE. These pairs require human review.",
        "clusters": review_cluster_list,
    }

    REVIEW_PATH.write_text(json.dumps(review_output, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Review file written: %s clusters in 0.85-0.97 band", len(review_cluster_list))

    # ------------------------------------------------------------------
    # 8. Summary
    # ------------------------------------------------------------------
    log.info("=" * 60)
    if dry_run:
        log.info("DRY RUN — no vault mutations performed")
    log.info("Auto-dedup clusters (>=%.2f): %d", THRESH_AUTO, clusters_deprecated)
    log.info("Memories auto-deprecated:     %d", mems_deprecated)
    log.info("Review pairs (%.2f-%.2f):      %d", THRESH_REVIEW, THRESH_AUTO, len(review_pairs))
    log.info("Review clusters (manual):     %d", len(review_cluster_list))
    log.info("Audit log:   %s (%d lines)", AUDIT_PATH, len(audit_records))
    log.info("Review file: %s", REVIEW_PATH)
    if FALLBACK_WARNING:
        log.warning("NOTE: Similarity computed via bigram Jaccard fallback, not embeddings.")
    log.info("=" * 60)

    # Print tail summary for verify step
    print(f"DONE: {clusters_deprecated} clusters auto-deprecated (>={THRESH_AUTO}), "
          f"{len(review_cluster_list)} clusters in manual review band "
          f"({THRESH_REVIEW}-{THRESH_AUTO}), "
          f"{len(audit_records)} audit records written")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Conservative vault dedup pass")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate deprecations without making vault mutations",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
