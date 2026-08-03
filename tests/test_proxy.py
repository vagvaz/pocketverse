"""Integration tests for pocketverse.proxy.

Runs without root.  Uses subprocess + asyncio (stdlib) — no pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from pocketverse.proxy import match_rule


# ===================================================================
# match_rule unit tests
# ===================================================================

class TestMatchRule:
    """Lexical domain matching (no DNS, no resolution)."""

    def test_exact_match(self):
        """A bare hostname rule matches exactly that hostname."""
        assert match_rule("example.com", 443, ["example.com"], [443, 80]) == "example.com"
        assert match_rule("example.com", 80, ["example.com"], [443, 80]) == "example.com"
        assert match_rule("example.org", 443, ["example.com"], [443, 80]) is None

    def test_wildcard_subdomain(self):
        """*.example.com matches strict subdomains but not the bare domain
        and not unrelated domains."""
        rules = ["*.example.com"]
        allow = [443, 80]

        # a.example.com is a strict subdomain → allowed
        assert match_rule("a.example.com", 443, rules, allow) is not None

        # The bare domain itself → denied
        assert match_rule("example.com", 443, rules, allow) is None

        # Unrelated suffix → denied
        assert match_rule("evil-a.example.com.evil.org", 443, rules, allow) is None

    def test_wildcard_deeper_subdomain(self):
        """*.example.com allows subdomains at any depth."""
        rules = ["*.example.com"]
        allow = [443, 80]
        assert match_rule("a.b.example.com", 443, rules, allow) is not None
        assert match_rule("x.y.z.example.com", 443, rules, allow) is not None

    def test_port_pin(self):
        """A rule with an explicit port only matches that port."""
        assert match_rule("example.com", 8443, ["example.com:8443"], [443, 80]) is not None
        # Same host but different port → denied
        assert match_rule("example.com", 443, ["example.com:8443"], [443, 80]) is None

    def test_wildcard_with_port_pin(self):
        """*.example.com:8443 matches subdomains only on that port."""
        rules = ["*.example.com:8443"]
        allow = [443, 80]
        assert match_rule("a.example.com", 8443, rules, allow) is not None
        assert match_rule("a.example.com", 443, rules, allow) is None

    def test_default_allow_ports(self):
        """Rules without port require the port to be in allow_ports."""
        # Port 443 is in default allow_ports → allowed
        assert match_rule("example.com", 443, ["example.com"], [443, 80]) is not None
        # Port 8080 is not in default allow_ports → denied
        assert match_rule("example.com", 8080, ["example.com"], [443, 80]) is None

    def test_case_insensitivity(self):
        """Hostname matching is case-insensitive."""
        assert match_rule("EXAMPLE.COM", 443, ["example.com"], [443, 80]) is not None
        assert match_rule("example.com", 443, ["EXAMPLE.COM"], [443, 80]) is not None

    def test_trailing_dot(self):
        """Trailing dots are stripped before comparison."""
        assert match_rule("example.com.", 443, ["example.com"], [443, 80]) is not None
        assert match_rule("example.com", 443, ["example.com."], [443, 80]) is not None

    def test_wildcard_trailing_dot(self):
        """Wildcard rules also strip trailing dots."""
        rules = ["*.example.com."]
        allow = [443, 80]
        assert match_rule("a.example.com.", 443, rules, allow) is not None
        assert match_rule("a.example.com", 443, rules, allow) is not None

    def test_ipv4_exact(self):
        """Literal IPv4 addresses match via exact comparison."""
        assert match_rule("192.168.1.1", 443, ["192.168.1.1"], [443, 80]) is not None
        assert match_rule("192.168.1.2", 443, ["192.168.1.1"], [443, 80]) is None


# ===================================================================
# Integration helpers
# ===================================================================

TMPDIR: tempfile.TemporaryDirectory | None = None


def _get_tmpdir() -> str:
    global TMPDIR
    if TMPDIR is None:
        TMPDIR = tempfile.mkdtemp(prefix="pocket_test_proxy_")
    return TMPDIR


def _free_port() -> int:
    """Return a currently-free TCP port (best-effort, TOCTOU possible)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _echo_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Echo everything read back to the writer."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _http_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Read a request and send a canned HTTP response."""
    try:
        await reader.read(65536)  # consume request
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 12\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"Hello World!"
        )
        writer.write(response)
        await writer.drain()
    finally:
        try:
            writer.close()
        except OSError:
            pass


def _wait_for_socket(path: str, timeout: float = 5.0) -> None:
    """Poll until *path* exists (a unix socket file)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.05)
    pytest.fail(f"socket {path} did not appear within {timeout}s")


def _start_proxy(
    sock_path: str,
    rules: list[str],
    allow_ports: list[int] | None = None,
    log_path: str | None = None,
) -> subprocess.Popen:
    """Start the proxy subprocess and wait for its socket."""
    cmd = [
        sys.executable,
        "-m",
        "pocketverse.proxy",
        "--sock",
        sock_path,
    ]
    for r in rules:
        cmd.extend(["--allow", r])
    for p in (allow_ports or [443, 80]):
        cmd.extend(["--allow-port", str(p)])
    if log_path is not None:
        cmd.extend(["--log", log_path])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_socket(sock_path)
    return proc


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


# ===================================================================
# Integration tests — CONNECT
# ===================================================================


class TestProxyConnect:
    """CONNECT tunnel through the proxy."""

    def test_connect_allowed(self):
        """Allowed CONNECT gets 200 and bytes round-trip."""
        sock = os.path.join(_get_tmpdir(), f"conn_allowed_{os.getpid()}.sock")
        echo_port = _free_port()

        # 1. Start proxy with the echo port allowed
        proc = _start_proxy(sock, rules=["127.0.0.1"], allow_ports=[echo_port])
        try:
            # 2. Run the echo server + test in asyncio
            async def run():
                # Start echo server on the pre-chosen port
                echo_server = await asyncio.start_server(
                    _echo_handler, host="127.0.0.1", port=echo_port
                )
                async with echo_server:
                    # Connect to proxy
                    r, w = await asyncio.open_unix_connection(sock)

                    # Send CONNECT
                    w.write(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\n".encode())
                    await w.drain()

                    # Read 200 response
                    resp = await asyncio.wait_for(
                        r.readuntil(b"\r\n\r\n"), timeout=5
                    )
                    assert b"200" in resp, f"Expected 200, got {resp!r}"

                    # Bidirectional echo test
                    payload = b"hello-proxy-connect"
                    w.write(payload)
                    await w.drain()

                    echoed = await asyncio.wait_for(
                        r.readexactly(len(payload)), timeout=5
                    )
                    assert echoed == payload, f"Expected {payload!r}, got {echoed!r}"

                    w.close()

            asyncio.run(run())
        finally:
            _stop_proc(proc)

    def test_connect_denied(self):
        """Denied CONNECT gets 403 and the log records a DENY line."""
        sock = os.path.join(_get_tmpdir(), f"conn_denied_{os.getpid()}.sock")
        log = os.path.join(_get_tmpdir(), f"conn_denied_{os.getpid()}.log")

        # Rules don't include 127.0.0.1
        proc = _start_proxy(sock, rules=["example.com"], log_path=log)
        try:
            async def run():
                r, w = await asyncio.open_unix_connection(sock)
                w.write(b"CONNECT 127.0.0.1:9999 HTTP/1.1\r\n\r\n")
                await w.drain()

                resp = await asyncio.wait_for(r.read(1024), timeout=5)
                assert b"403" in resp or b"Forbidden" in resp, \
                    f"Expected 403, got {resp!r}"
                w.close()

            asyncio.run(run())

            # Verify log
            with open(log) as f:
                lines = f.read()
            assert "DENY" in lines, f"Log missing DENY: {lines}"
            assert "127.0.0.1:9999" in lines, f"Log missing target: {lines}"
            assert "no-match" in lines, f"Log missing reason: {lines}"
        finally:
            _stop_proc(proc)


# ===================================================================
# Integration tests — Plain HTTP proxy
# ===================================================================


class TestProxyHttp:
    """Absolute-URI plain HTTP proxy requests."""

    def test_plain_http_allowed(self):
        """Allowed HTTP absolute-URI request returns the upstream response."""
        sock = os.path.join(_get_tmpdir(), f"http_allowed_{os.getpid()}.sock")
        http_port = _free_port()

        proc = _start_proxy(sock, rules=["127.0.0.1"], allow_ports=[http_port])
        try:
            async def run():
                http_server = await asyncio.start_server(
                    _http_handler, host="127.0.0.1", port=http_port
                )
                async with http_server:
                    r, w = await asyncio.open_unix_connection(sock)

                    request = (
                        f"GET http://127.0.0.1:{http_port}/test HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{http_port}\r\n"
                        f"Connection: close\r\n"
                        f"\r\n"
                    ).encode()

                    w.write(request)
                    await w.drain()

                    response = await asyncio.wait_for(r.read(4096), timeout=5)
                    assert b"200 OK" in response, \
                        f"Expected 200 OK, got {response!r}"
                    assert b"Hello World!" in response, \
                        f"Expected body, got {response!r}"
                    w.close()

            asyncio.run(run())
        finally:
            _stop_proc(proc)


# ===================================================================
# Resilience tests
# ===================================================================


class TestProxyResilience:
    """Proxy must survive bad input."""

    def test_malformed_garbage(self):
        """Garbage input doesn't crash the proxy; subsequent connections work."""
        sock = os.path.join(_get_tmpdir(), f"garbage_{os.getpid()}.sock")
        proc = _start_proxy(sock, rules=["example.com"])
        try:
            async def run():
                # Send garbage
                r, w = await asyncio.open_unix_connection(sock)
                w.write(b"\x00\x01\x02\x03random garbage\r\n")
                await w.drain()

                # Should get 403 or connection close
                try:
                    resp = await asyncio.wait_for(r.read(1024), timeout=2)
                    assert b"403" in resp or b"Forbidden" in resp
                except (asyncio.TimeoutError, OSError):
                    pass
                finally:
                    w.close()

                # Give proxy a moment to recover
                await asyncio.sleep(0.2)

                # Connect again — proxy must still be alive
                r2, w2 = await asyncio.open_unix_connection(sock)
                w2.write(b"garbage again\r\n")
                await w2.drain()
                try:
                    resp2 = await asyncio.wait_for(r2.read(1024), timeout=2)
                    assert b"403" in resp2 or b"Forbidden" in resp2
                except (asyncio.TimeoutError, OSError):
                    pass
                finally:
                    w2.close()

            asyncio.run(run())
        finally:
            _stop_proc(proc)


# ===================================================================
# Cleanup
# ===================================================================


def pytest_unconfigure(config):  # noqa: ARG001
    """Clean up global temp dir."""
    global TMPDIR
    if TMPDIR is not None:
        import shutil

        shutil.rmtree(TMPDIR, ignore_errors=True)
        TMPDIR = None
