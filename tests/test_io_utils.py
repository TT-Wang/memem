"""Tests for memem.io_utils.atomic_write_text / atomic_write_bytes."""

import os
import threading
from pathlib import Path

import pytest

from memem.io_utils import atomic_write_bytes, atomic_write_text


def test_atomic_write_text_basic(tmp_path: Path):
    """Plain happy path: file gets the content."""
    p = tmp_path / "out.txt"
    atomic_write_text(p, "hello world")
    assert p.read_text() == "hello world"


def test_atomic_write_creates_parent_dir(tmp_path: Path):
    """Missing parent dir gets created (make_parents=True default)."""
    p = tmp_path / "a" / "b" / "c.txt"
    atomic_write_text(p, "deep")
    assert p.read_text() == "deep"


def test_atomic_write_overwrites_existing(tmp_path: Path):
    """Overwriting an existing file: reader sees old OR new, never partial."""
    p = tmp_path / "f.txt"
    p.write_text("old content")
    atomic_write_text(p, "new content")
    assert p.read_text() == "new content"


def test_atomic_write_no_tmp_left_on_success(tmp_path: Path):
    """After successful write, no stray tempfiles should remain in the dir."""
    p = tmp_path / "clean.txt"
    atomic_write_text(p, "ok")
    tmpfiles = [f for f in tmp_path.iterdir() if f.name.startswith(".clean.txt.")]
    assert tmpfiles == []


def test_atomic_write_cleans_tmp_on_failure(tmp_path: Path, monkeypatch):
    """Forced replace failure must clean up the tempfile."""
    p = tmp_path / "fail.txt"

    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_text(p, "wont land")

    tmpfiles = [f for f in tmp_path.iterdir() if f.name.startswith(".fail.txt.")]
    assert tmpfiles == [], "tempfile must be cleaned up after failed replace"


def test_atomic_write_bytes(tmp_path: Path):
    """Binary payload writes the exact bytes."""
    p = tmp_path / "raw.bin"
    payload = b"\x00\x01\x02\xff"
    atomic_write_bytes(p, payload)
    assert p.read_bytes() == payload


def test_atomic_write_concurrent_overwrite_no_corruption(tmp_path: Path):
    """N threads racing to write the same path: final content must be one
    full payload, never a torn mix."""
    p = tmp_path / "race.txt"
    payloads = [f"payload-{i:04d}-" + ("x" * 1000) for i in range(20)]

    def writer(idx: int):
        atomic_write_text(p, payloads[idx])

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = p.read_text()
    # The final content must be exactly one of the payloads (atomic rename)
    assert final in payloads, "final content is not any single payload — torn write!"


def test_atomic_write_fsync_disabled_via_env(tmp_path: Path, monkeypatch):
    """MEMEM_FSYNC=0 disables fsync (visible via skipping the call)."""
    monkeypatch.setenv("MEMEM_FSYNC", "0")
    # Reload the module so the env override takes effect at module load
    import importlib

    import memem.io_utils as mod
    importlib.reload(mod)

    fsync_calls = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    mod.atomic_write_text(tmp_path / "no-sync.txt", "data")
    assert fsync_calls == [], "fsync should be skipped when MEMEM_FSYNC=0"

    # Restore default for the rest of the test session
    monkeypatch.setenv("MEMEM_FSYNC", "1")
    importlib.reload(mod)
