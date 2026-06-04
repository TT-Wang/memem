"""Tests for H-2: WAL + busy_timeout PRAGMAs on graph_index and search_index.

session_state_db already had WAL since v1.6 — this test guards the v1.9.3
addition for the other two DBs and verifies all three behave consistently.
"""

import sqlite3
from pathlib import Path


def _pragma(path: Path, name: str):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        conn.close()


def test_search_index_uses_wal(tmp_cortex_dir):
    """_init_search_db must put the DB in WAL mode."""
    from memem.search_index import _init_search_db
    conn = _init_search_db()
    conn.close()

    from memem.models import MEMEM_DIR
    db_path = MEMEM_DIR / "search.db"
    assert db_path.exists()
    assert _pragma(db_path, "journal_mode").lower() == "wal"


def test_graph_index_uses_wal(tmp_cortex_dir):
    """_init_graph_db must put the DB in WAL mode."""
    from memem.graph_index import _init_graph_db
    conn = _init_graph_db()
    conn.close()

    from memem.models import MEMEM_DIR
    db_path = MEMEM_DIR / "graph.db"
    assert db_path.exists()
    assert _pragma(db_path, "journal_mode").lower() == "wal"


def test_search_index_sets_busy_timeout(tmp_cortex_dir):
    """busy_timeout must be set so short contention doesn't immediately fail."""
    from memem.search_index import _init_search_db
    conn = _init_search_db()
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert timeout >= 5000, f"busy_timeout too low: {timeout}ms"


def test_graph_index_sets_busy_timeout(tmp_cortex_dir):
    from memem.graph_index import _init_graph_db
    conn = _init_graph_db()
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert timeout >= 5000


def test_integrity_check_helper_on_fresh_install(tmp_cortex_dir, capsys):
    """_run_integrity_check must report 'no failures' on an empty MEMEM_DIR
    (no DBs present yet), not falsely report corruption."""
    from memem.cli import _run_integrity_check
    failed = _run_integrity_check(verbose=False)
    assert failed is False


def test_integrity_check_helper_on_healthy_db(tmp_cortex_dir):
    """After initializing both WAL DBs, integrity_check must pass."""
    from memem.graph_index import _init_graph_db
    from memem.search_index import _init_search_db
    _init_search_db().close()
    _init_graph_db().close()

    from memem.cli import _run_integrity_check
    failed = _run_integrity_check(verbose=False)
    assert failed is False
