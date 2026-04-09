"""
Retrieval benchmark for Cortex memory search.

Stores N memories, queries with M goals, measures:
- Recall@5: % of relevant memories found in top 5 results
- Precision@5: % of top 5 results that are relevant
- MRR: Mean Reciprocal Rank of first relevant result
"""
import os
import sys
import json
import shutil
import tempfile
from pathlib import Path

# Point to a temp cortex dir for benchmarking
BENCHMARK_DIR = tempfile.mkdtemp(prefix="cortex_bench_")
os.environ["CORTEX_DIR"] = BENCHMARK_DIR

# Now import server (which uses CORTEX_DIR)
sys.path.insert(0, os.path.dirname(__file__))
from server import memory_save, _search_memories, _all_memories, MEMORIES_DIR, LOGS_DIR

MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# Test dataset: (memory_content, memory_title, tags, relevant_queries)
DATASET = [
    # Auth domain
    ("Use RS256 algorithm for JWT signing in production environments", "JWT signing algorithm", "auth,jwt",
     ["JWT authentication", "token signing", "auth system design"]),

    ("Always validate JWT token expiry on the server side, never trust client", "JWT expiry validation", "auth,jwt,security",
     ["token validation", "JWT security", "auth middleware"]),

    ("OAuth 2.1 is recommended over OAuth 2.0 for new implementations", "OAuth version", "auth,oauth",
     ["OAuth implementation", "auth protocol choice"]),

    ("Store password hashes with bcrypt, never store plain text passwords", "Password storage", "auth,security",
     ["password hashing", "secure password storage", "user authentication"]),

    # Database domain
    ("Always use database migrations instead of direct ALTER TABLE commands", "Database migrations", "database",
     ["schema changes", "database updates", "migration strategy"]),

    ("Add indexes on columns used in WHERE, JOIN, and ORDER BY clauses", "Database indexing", "database,performance",
     ["query optimization", "database performance", "slow queries"]),

    ("Use connection pooling to manage database connections efficiently", "Connection pooling", "database,performance",
     ["database connections", "pool configuration", "connection management"]),

    # Testing domain
    ("Write tests that verify behavior, not implementation details", "Test behavior", "testing",
     ["unit testing strategy", "test design", "refactoring tests"]),

    ("Use pytest fixtures for test setup, prefer factory functions over shared state", "Pytest fixtures", "testing,python",
     ["pytest setup", "test fixtures", "python testing"]),

    ("Mock external dependencies but never mock the system under test", "Mocking strategy", "testing",
     ["mock usage", "test doubles", "integration testing"]),

    # API domain
    ("Use consistent naming conventions across all API endpoints", "API naming", "api,design",
     ["REST API design", "endpoint naming", "API conventions"]),

    ("Return appropriate HTTP status codes: 201 for created, 404 for not found", "HTTP status codes", "api,http",
     ["error responses", "API status codes", "REST responses"]),

    # Deployment
    ("Use environment variables for configuration, never hardcode secrets", "Configuration management", "deployment,security",
     ["env variables", "secret management", "deployment config"]),

    ("Set up health check endpoints for monitoring service availability", "Health checks", "deployment,monitoring",
     ["service monitoring", "availability checks", "deployment health"]),

    # General
    ("Prefer small, focused commits over large multi-purpose commits", "Commit strategy", "git",
     ["git workflow", "commit best practices", "version control"]),
]

# Irrelevant queries (should NOT match well)
IRRELEVANT_QUERIES = [
    "chocolate cake recipe",
    "weather forecast tomorrow",
    "best movies of 2025",
    "yoga exercises for beginners",
]


def run_benchmark():
    """Run the retrieval benchmark."""
    print("=" * 60)
    print("  CORTEX RETRIEVAL BENCHMARK")
    print("=" * 60)
    print()

    # 1. Store all memories
    print(f"Storing {len(DATASET)} memories...")
    for content, title, tags, _ in DATASET:
        memory_save(content=content, title=title, tags=tags, scope_id="bench")

    total_mems = len(_all_memories("bench"))
    print(f"Stored: {total_mems} memories")
    print()

    # 2. Test relevant queries
    print("Testing relevant queries...")
    total_recall = 0
    total_precision = 0
    total_mrr = 0
    query_count = 0

    for content, title, tags, queries in DATASET:
        for query in queries:
            results = _search_memories(query, scope_id="bench", limit=5)
            result_titles = [r.get("title", "") for r in results]

            # Check if the target memory is in results
            found = title in result_titles
            rank = result_titles.index(title) + 1 if found else 0

            # Recall@5: was it found?
            total_recall += 1 if found else 0

            # Precision@5: how many results are from the same domain?
            target_domain_tags = set(tags.split(","))
            relevant_count = 0
            for r in results:
                r_tags = set(r.get("domain_tags", []))
                if r_tags & target_domain_tags:
                    relevant_count += 1
            total_precision += relevant_count / max(len(results), 1)

            # MRR
            total_mrr += (1.0 / rank) if rank > 0 else 0

            query_count += 1

            status = "✓" if found else "✗"
            print(f"  {status} \"{query}\" → {'found at rank ' + str(rank) if found else 'NOT FOUND'}")

    print()

    # 3. Test irrelevant queries (should return low-scoring or no results)
    print("Testing irrelevant queries...")
    false_positives = 0
    for query in IRRELEVANT_QUERIES:
        results = _search_memories(query, scope_id="bench", limit=5)
        if results:
            top_score = results[0].get("confidence", 0)
            print(f"  ⚠ \"{query}\" returned {len(results)} results")
            false_positives += len(results)
        else:
            print(f"  ✓ \"{query}\" correctly returned no results")

    print()

    # 4. Summary
    recall_at_5 = total_recall / query_count if query_count else 0
    precision_at_5 = total_precision / query_count if query_count else 0
    mrr = total_mrr / query_count if query_count else 0

    print("=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  Queries tested:     {query_count}")
    print(f"  Recall@5:           {recall_at_5:.1%}")
    print(f"  Precision@5:        {precision_at_5:.1%}")
    print(f"  MRR:                {mrr:.3f}")
    print(f"  False positives:    {false_positives}")
    print(f"  Embeddings:         {'yes (semantic)' if _check_embeddings() else 'no (keyword only)'}")
    print("=" * 60)

    # Cleanup
    shutil.rmtree(BENCHMARK_DIR, ignore_errors=True)

    return recall_at_5, precision_at_5, mrr


def _check_embeddings():
    try:
        from embeddings import is_available
        return is_available()
    except Exception:
        return False


if __name__ == "__main__":
    run_benchmark()
