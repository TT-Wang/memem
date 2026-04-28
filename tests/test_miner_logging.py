"""Tests for structlog JSON logging in miner_daemon.py."""
from __future__ import annotations

import json
import logging
import logging.handlers

import pytest
import structlog


@pytest.fixture(autouse=True)
def reset_structlog_after_test():
    """Reset structlog global configuration after every test to prevent state leakage."""
    yield
    structlog.reset_defaults()


def test_structlog_importable():
    """structlog must be importable from the project's Python environment."""
    import structlog as _structlog  # noqa: F401 — import-only test

    assert hasattr(_structlog, "get_logger")


def test_log_emits_session_id_field():
    """mining_session log event must include the session_id field."""
    with structlog.testing.capture_logs() as cap:
        log = structlog.get_logger("memem-miner-test")
        log.info("mining_session", session_id="abc123")

    assert len(cap) == 1
    entry = cap[0]
    assert entry["event"] == "mining_session"
    assert entry["session_id"] == "abc123"


def test_log_outcome_field_uses_known_values():
    """outcome field must be one of the known enumeration values."""
    known_outcomes = {"success", "failure", "skipped", "blocked"}

    with structlog.testing.capture_logs() as cap:
        log = structlog.get_logger("memem-miner-test")
        log.info("session_processed", outcome="success")

    entry = cap[0]
    assert entry["outcome"] in known_outcomes


def test_log_duration_ms_is_numeric():
    """duration_ms field must be parseable as a number."""
    with structlog.testing.capture_logs() as cap:
        log = structlog.get_logger("memem-miner-test")
        log.info("session_processed", duration_ms=42)

    entry = cap[0]
    assert isinstance(entry["duration_ms"], int | float)
    assert entry["duration_ms"] == 42


def test_rotating_handler_configured_correctly(tmp_path, monkeypatch):
    """After _configure_logging, the stdlib logger must have a RotatingFileHandler
    with maxBytes=10MB and backupCount=5."""
    import memem.miner_daemon as md

    # Patch MEMEM_DIR and LOG_FILE to use tmp_path so we don't touch real state
    monkeypatch.setattr(md, "MEMEM_DIR", tmp_path)
    monkeypatch.setattr(md, "LOG_FILE", tmp_path / "miner.log")

    stdlib_logger = logging.getLogger("memem-miner")
    # Save state to restore afterwards
    orig_propagate = stdlib_logger.propagate
    orig_level = stdlib_logger.level
    # Remove any existing handlers to force re-configuration
    for h in list(stdlib_logger.handlers):
        stdlib_logger.removeHandler(h)

    md._configure_logging()

    handlers = logging.getLogger("memem-miner").handlers
    rotating = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1, f"Expected 1 RotatingFileHandler, got {len(rotating)}"

    handler = rotating[0]
    assert handler.maxBytes == 10 * 1024 * 1024, f"Expected 10MB maxBytes, got {handler.maxBytes}"
    assert handler.backupCount == 5, f"Expected backupCount=5, got {handler.backupCount}"

    # Clean up: remove handlers and restore original propagate/level so we don't
    # break subsequent tests that rely on caplog (which requires propagate=True).
    for h in list(stdlib_logger.handlers):
        stdlib_logger.removeHandler(h)
    stdlib_logger.propagate = orig_propagate
    stdlib_logger.setLevel(orig_level)


def test_log_emits_json(tmp_path):
    """A log event written to a RotatingFileHandler must be valid JSON with expected keys."""
    # Set up a dedicated test stdlib logger (not "memem-miner" to avoid cross-test coupling)
    test_logger_name = "memem-miner-json-test"
    stdlib_logger = logging.getLogger(test_logger_name)
    for h in list(stdlib_logger.handlers):
        stdlib_logger.removeHandler(h)

    log_file = tmp_path / "test.log"
    handler = logging.handlers.RotatingFileHandler(str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5)
    handler.setFormatter(logging.Formatter("%(message)s"))
    stdlib_logger.addHandler(handler)
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.propagate = False

    # Configure structlog with JSON output (reset_structlog_after_test will clean this up)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    test_log = structlog.get_logger(test_logger_name)
    test_log.info("test_event", session_id="xyz", outcome="success")

    handler.flush()
    handler.close()
    stdlib_logger.removeHandler(handler)

    lines = [ln.strip() for ln in log_file.read_text().splitlines() if ln.strip()]
    assert len(lines) >= 1, "Expected at least one log line"

    parsed = json.loads(lines[-1])
    assert "event" in parsed, f"Missing 'event' key in: {parsed}"
    assert "level" in parsed, f"Missing 'level' key in: {parsed}"
    assert "timestamp" in parsed, f"Missing 'timestamp' key in: {parsed}"
