"""Slice client — thin client for talking to slice_daemon over a Unix socket.

Public API: :func:`try_slice_via_daemon`

Designed for use inside hooks (auto-recall.sh embeds this in a ``python3 -``
heredoc). The function *never raises* — any failure returns ``None``, allowing
the caller to fall back to the subprocess path.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path


def _get_sock_path() -> Path:
    """Return the Unix socket path, honouring MEMEM_DIR env override."""
    import os
    memem_dir = os.environ.get("MEMEM_DIR") or os.environ.get("CORTEX_DIR") or str(Path.home() / ".memem")
    return Path(memem_dir) / "slice.sock"


def try_slice_via_daemon(
    query: str,
    scope: str,
    session_id: str = "",
    cwd: str = "",
    task_mode: str = "",
    use_llm: bool = False,
    timeout_seconds: float = 5.0,
    sock_path: str | Path | None = None,
) -> str | None:
    """Try to get a memory slice from the running slice_daemon over Unix socket.

    Returns the rendered slice string on success.
    Returns ``None`` on ANY failure (socket missing, connect refused, timeout,
    protocol error, daemon-reported error). Caller must fall back to the
    subprocess path.

    Never raises. This is best-effort; the slow subprocess path is the
    correctness backstop.

    Parameters
    ----------
    query:          User's current message.
    scope:          Project scope identifier.
    session_id:     Claude Code session ID (optional).
    cwd:            Working directory (optional).
    task_mode:      Task mode hint (optional).
    use_llm:        Whether to use LLM activation. Default False (fast path).
    timeout_seconds: Socket connect + read timeout. Default 5.0s.
    sock_path:      Override socket path (for tests).
    """
    try:
        effective_sock = Path(sock_path) if sock_path else _get_sock_path()
        if not effective_sock.exists():
            return None

        payload = json.dumps({
            "query": query,
            "scope": scope,
            "session_id": session_id,
            "cwd": cwd,
            "task_mode": task_mode,
            "use_llm": use_llm,
        }) + "\n"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_seconds)
            sock.connect(str(effective_sock))
            sock.sendall(payload.encode("utf-8"))

            # Read until newline
            buf = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    break

        if not buf:
            return None

        raw = buf.decode("utf-8", errors="replace").rstrip("\n").strip()
        resp = json.loads(raw)

        if not isinstance(resp, dict):
            return None
        if not resp.get("ok"):
            return None

        result = resp.get("slice")
        if not isinstance(result, str):
            return None

        return result or None

    except Exception:
        return None
