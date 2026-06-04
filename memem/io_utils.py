"""Atomic file-write primitives shared across memem.

`atomic_write_text` and `atomic_write_bytes` write to a sibling tempfile,
fsync the data, then `os.replace` into the target path. This rules out
torn-write corruption from power loss, NFS latency, or buggy subprocess
kills mid-write.

Why a shared module: as of v1.9.2 the atomic-write pattern lived inline in
four places (working_memory.py, telemetry.py, delta_commit.py, feedback.py)
and five real data-write sites used plain `path.write_text`. v1.9.3
consolidates the pattern here and applies it to the five non-atomic sites.

fsync can be disabled with MEMEM_FSYNC=0 (default on). Most users should
leave it alone; the env knob exists for shops with bursty workloads where
the durability/throughput tradeoff is unfavourable.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_FSYNC_ENABLED = os.environ.get("MEMEM_FSYNC", "1") != "0"


def atomic_write_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    encoding: str = "utf-8",
    fsync: bool | None = None,
    make_parents: bool = True,
) -> None:
    """Write `content` to `path` atomically.

    The write goes to a sibling tempfile, fsyncs the file (if enabled), then
    `os.replace`s into the target. POSIX `rename` semantics guarantee that
    a reader either sees the prior file contents or the new ones — never a
    partial write.

    If `make_parents` is True (default), the parent directory is created if
    it does not exist.

    `fsync=None` defers to the module-level default (env var MEMEM_FSYNC).
    Set it explicitly to override per-call.
    """
    p = Path(path)
    if make_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    do_fsync = _FSYNC_ENABLED if fsync is None else fsync

    fd, tmp_str = tempfile.mkstemp(
        dir=p.parent,
        prefix=f".{p.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
            if do_fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_str, p)
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


def atomic_write_bytes(
    path: str | os.PathLike[str],
    content: bytes,
    *,
    fsync: bool | None = None,
    make_parents: bool = True,
) -> None:
    """Same as atomic_write_text but for binary payloads."""
    p = Path(path)
    if make_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    do_fsync = _FSYNC_ENABLED if fsync is None else fsync

    fd, tmp_str = tempfile.mkstemp(
        dir=p.parent,
        prefix=f".{p.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            if do_fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_str, p)
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise
