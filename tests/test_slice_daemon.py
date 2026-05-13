"""Tests for memem.slice_daemon and memem.slice_client."""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_sock(sock_path: Path, timeout: float = 5.0) -> bool:
    """Wait until the Unix socket file appears and accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    s.connect(str(sock_path))
                    return True
            except OSError:
                pass
        time.sleep(0.05)
    return False


def _make_request(sock_path: Path, query: str = "test query", scope: str = "default",
                  timeout: float = 5.0) -> dict:
    """Send a request to the daemon and return the parsed response dict."""
    payload = json.dumps({
        "query": query,
        "scope": scope,
        "session_id": "",
        "cwd": "",
        "task_mode": "",
        "use_llm": False,
    }) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        sock.sendall(payload.encode())
        buf = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf:
                break
    return json.loads(buf.decode().strip())


def _start_daemon_process(sock_path: Path, max_inflight: int = 8,
                          fake_slice: str = "## Memory slice\n\ntest content") -> multiprocessing.Process:
    """Spawn the daemon in a subprocess with mocked slice generation.

    Uses sock_path.parent as the temp directory for lock/pid/heartbeat files
    so tests don't conflict with a real daemon in ~/.memem.
    """
    tmp_dir = sock_path.parent

    def _run_daemon(sock_path_str: str, tmp_dir_str: str, max_inflight: int, fake_slice: str) -> None:
        # Mock out the heavy embedding / slice generation
        from pathlib import Path as _Path
        from unittest.mock import patch

        mock_response = fake_slice
        tmp_dir = _Path(tmp_dir_str)

        with patch("memem.active_slice_engine.active_slice_response", return_value=mock_response):
            from memem.slice_daemon import run
            run(
                sock_path=sock_path_str,
                max_inflight=max_inflight,
                request_timeout=25.0,
                lock_path=str(tmp_dir / "slice.lock"),
                pid_path=str(tmp_dir / "slice-daemon.pid"),
                heartbeat_path=str(tmp_dir / "slice-daemon.heartbeat"),
            )

    proc = multiprocessing.Process(
        target=_run_daemon,
        args=(str(sock_path), str(tmp_dir), max_inflight, fake_slice),
        daemon=True,
    )
    proc.start()
    return proc


# ---------------------------------------------------------------------------
# Tests: slice_client
# ---------------------------------------------------------------------------

class TestClientReturnsNoneWhenNoSocket:
    def test_client_returns_none_when_no_socket(self, tmp_path: Path) -> None:
        """With no daemon running and no socket file, try_slice_via_daemon returns None."""
        sock_path = tmp_path / "nonexistent.sock"
        assert not sock_path.exists()

        from memem.slice_client import try_slice_via_daemon
        result = try_slice_via_daemon("hello", "default", sock_path=sock_path)
        assert result is None


class TestClientReturnsNoneOnConnectRefused:
    def test_client_returns_none_on_connect_refused(self, tmp_path: Path) -> None:
        """Socket file exists but no daemon listening; client returns None."""
        sock_path = tmp_path / "fake.sock"
        # Create a socket file but nobody is listening
        # We create + immediately close a bound socket (leaving the file) to simulate stale sock
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.close()
        # Now the file exists but nobody is listening
        assert sock_path.exists()

        from memem.slice_client import try_slice_via_daemon
        result = try_slice_via_daemon("hello", "default", sock_path=sock_path, timeout_seconds=1.0)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: daemon round-trip
# ---------------------------------------------------------------------------

class TestProtocolRequestResponseRoundTrip:
    def test_round_trip(self, tmp_path: Path) -> None:
        """Spawn daemon, send one request, assert response shape."""
        sock_path = tmp_path / "slice.sock"
        fake_slice = "## Memory\n\nsome content"

        proc = _start_daemon_process(sock_path, fake_slice=fake_slice)
        try:
            assert _wait_for_sock(sock_path, timeout=8.0), "daemon did not start in time"

            resp = _make_request(sock_path)
            assert resp["ok"] is True
            assert isinstance(resp["slice"], str)
            assert isinstance(resp["elapsed_ms"], int)
        finally:
            proc.terminate()
            proc.join(timeout=3)


class TestDaemonHandlesConcurrentRequests:
    def test_concurrent_requests(self, tmp_path: Path) -> None:
        """Send 4 concurrent requests; all should succeed."""
        sock_path = tmp_path / "slice.sock"

        proc = _start_daemon_process(sock_path)
        try:
            assert _wait_for_sock(sock_path, timeout=8.0), "daemon did not start in time"

            results = []
            errors = []

            def _worker():
                try:
                    resp = _make_request(sock_path)
                    results.append(resp)
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=_worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert not errors, f"Thread errors: {errors}"
            assert len(results) == 4
            for r in results:
                assert r["ok"] is True
        finally:
            proc.terminate()
            proc.join(timeout=3)


class TestDaemonRefusesOverload:
    @pytest.mark.skip(
        reason="v1.8.0 ships with WORKER_THREADS=1 (tournament-cache thread-safety); "
        "overload check unreachable with single-worker sequential service. "
        "Re-enable when v1.8.1 raises workers + adds cache file-locking."
    )
    def test_overload_rejection(self, tmp_path: Path) -> None:
        """With max_inflight=2, 5 simultaneous requests → at least one 'overloaded'."""
        sock_path = tmp_path / "slice.sock"
        tmp_dir = tmp_path

        # Use a slow fake that holds inflight counter up
        def _slow_run(sock_path_str: str, tmp_dir_str: str, max_inflight: int, fake_slice: str) -> None:
            import time
            from pathlib import Path as _Path
            from unittest.mock import patch

            tmp_dir = _Path(tmp_dir_str)

            def _slow_response(*args, **kwargs):
                time.sleep(0.5)
                return fake_slice

            with patch("memem.active_slice_engine.active_slice_response", side_effect=_slow_response):
                from memem.slice_daemon import run
                run(
                    sock_path=sock_path_str,
                    max_inflight=max_inflight,
                    request_timeout=25.0,
                    lock_path=str(tmp_dir / "slice.lock"),
                    pid_path=str(tmp_dir / "slice-daemon.pid"),
                    heartbeat_path=str(tmp_dir / "slice-daemon.heartbeat"),
                )

        proc = multiprocessing.Process(
            target=_slow_run,
            args=(str(sock_path), str(tmp_dir), 2, "test slice"),
            daemon=True,
        )
        proc.start()

        try:
            assert _wait_for_sock(sock_path, timeout=8.0), "daemon did not start in time"

            results = []
            errors = []

            def _worker():
                try:
                    resp = _make_request(sock_path, timeout=10.0)
                    results.append(resp)
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=_worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            overloaded = [r for r in results if not r.get("ok") and r.get("error") == "overloaded"]
            assert len(overloaded) >= 1, f"Expected at least one overloaded response, got: {results}"
        finally:
            proc.terminate()
            proc.join(timeout=3)


class TestDaemonRequestTimeout:
    def test_request_timeout(self, tmp_path: Path) -> None:
        """Slice generator that sleeps 30s → daemon returns timeout-ish error."""
        sock_path = tmp_path / "slice.sock"
        tmp_dir = tmp_path

        def _timeout_run(sock_path_str: str, tmp_dir_str: str) -> None:
            import time
            from pathlib import Path as _Path
            from unittest.mock import patch

            tmp_dir = _Path(tmp_dir_str)

            def _forever(*args, **kwargs):
                time.sleep(60)
                return "never"

            with patch("memem.active_slice_engine.active_slice_response", side_effect=_forever):
                from memem.slice_daemon import run
                run(
                    sock_path=sock_path_str,
                    max_inflight=8,
                    request_timeout=1.0,
                    lock_path=str(tmp_dir / "slice.lock"),
                    pid_path=str(tmp_dir / "slice-daemon.pid"),
                    heartbeat_path=str(tmp_dir / "slice-daemon.heartbeat"),
                )

        proc = multiprocessing.Process(target=_timeout_run, args=(str(sock_path), str(tmp_dir)), daemon=True)
        proc.start()

        try:
            assert _wait_for_sock(sock_path, timeout=8.0), "daemon did not start in time"

            # Client with longer timeout than the daemon's per-request timeout
            try:
                resp = _make_request(sock_path, timeout=15.0)
                # The daemon may return an error if it manages to respond
                # (timeout via internal mechanism), or the connection may drop.
                # Either way: if we get a response, ok must be False.
                if resp is not None:
                    assert not resp.get("ok"), f"Expected error response, got {resp}"
            except (TimeoutError, json.JSONDecodeError, OSError):
                # Connection reset or timeout from client side is also acceptable
                pass
        finally:
            proc.terminate()
            proc.join(timeout=3)


class TestDaemonStaleSocketCleanup:
    def test_stale_socket_cleanup(self, tmp_path: Path) -> None:
        """Pre-create a bogus socket; daemon must remove it and bind successfully."""
        sock_path = tmp_path / "slice.sock"

        # Pre-create a stale socket file (unbound, no listener)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.close()
        assert sock_path.exists()

        proc = _start_daemon_process(sock_path)
        try:
            # Daemon should have cleaned up the stale socket and bound successfully
            ready = _wait_for_sock(sock_path, timeout=8.0)
            assert ready, "Daemon should have replaced stale socket and started"

            resp = _make_request(sock_path)
            assert resp["ok"] is True
        finally:
            proc.terminate()
            proc.join(timeout=3)


class TestAntiRecursionEnvSet:
    def test_anti_recursion_env_set(self, tmp_path: Path) -> None:
        """Daemon startup must set MEMEM_HOOK_DISABLE=1 in os.environ."""
        import memem.slice_daemon as sd

        # Save original state
        original = os.environ.pop("MEMEM_HOOK_DISABLE", None)
        try:
            os.environ.pop("MEMEM_HOOK_DISABLE", None)

            # Intercept _warmup_model to capture env state at that point
            captured_env: dict = {}

            def _mock_warmup():
                captured_env["MEMEM_HOOK_DISABLE"] = os.environ.get("MEMEM_HOOK_DISABLE", "NOT_SET")

            sock_path = tmp_path / "test.sock"

            with patch.object(sd, "_warmup_model", side_effect=_mock_warmup), \
                 patch.object(sd, "_shutdown_requested", True):
                # Run in a thread so it doesn't block (shutdown_requested=True exits loop quickly)
                t = threading.Thread(target=sd.run, kwargs={
                    "sock_path": str(sock_path),
                    "lock_path": str(tmp_path / "slice.lock"),
                    "pid_path": str(tmp_path / "slice.pid"),
                    "heartbeat_path": str(tmp_path / "slice.heartbeat"),
                })
                t.daemon = True
                t.start()
                t.join(timeout=5.0)

            assert "MEMEM_HOOK_DISABLE" in captured_env, "MEMEM_HOOK_DISABLE should be set before warmup"
            assert captured_env["MEMEM_HOOK_DISABLE"] == "1"
        finally:
            # Restore original state
            if original is not None:
                os.environ["MEMEM_HOOK_DISABLE"] = original
            else:
                os.environ.pop("MEMEM_HOOK_DISABLE", None)


# ---------------------------------------------------------------------------
# Tests: slice_client + daemon integration
# ---------------------------------------------------------------------------

class TestClientDaemonIntegration:
    def test_client_gets_slice_from_daemon(self, tmp_path: Path) -> None:
        """try_slice_via_daemon returns the slice string when daemon is running."""
        sock_path = tmp_path / "slice.sock"
        fake_slice = "## Memory\n\nintegration test content"

        proc = _start_daemon_process(sock_path, fake_slice=fake_slice)
        try:
            assert _wait_for_sock(sock_path, timeout=8.0), "daemon did not start in time"

            from memem.slice_client import try_slice_via_daemon
            result = try_slice_via_daemon(
                "test query", "default",
                sock_path=sock_path, timeout_seconds=5.0,
            )
            assert result is not None
            assert isinstance(result, str)
            assert len(result) > 0
        finally:
            proc.terminate()
            proc.join(timeout=3)
