"""Integration tests for pocketverse.shim.

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

TMPDIR: tempfile.TemporaryDirectory | None = None


def _get_tmpdir() -> str:
    global TMPDIR
    if TMPDIR is None:
        TMPDIR = tempfile.mkdtemp(prefix="pocket_test_shim_")
    return TMPDIR


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _unix_echo_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Echo everything back on a unix socket."""
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


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


# ===================================================================
# Integration tests
# ===================================================================


class TestShimRoundtrip:
    """TCP → shim → unix-socket → echo-server → bytes round-trip."""

    def test_bytes_round_trip(self):
        """Bytes written to the shim TCP port are echoed back via unix socket."""
        sock_path = os.path.join(_get_tmpdir(), f"echo_{os.getpid()}.sock")
        shim_port = _free_port()

        proc_shim: subprocess.Popen | None = None

        async def run():
            nonlocal proc_shim

            # 1. Remove any stale socket
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass

            # 2. Start unix-socket echo server
            echo_server = await asyncio.start_unix_server(
                _unix_echo_handler, path=sock_path
            )
            os.chmod(sock_path, 0o666)

            # 3. Start shim pointing at the echo socket
            proc_shim = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "pocketverse.shim",
                    "--port",
                    str(shim_port),
                    "--sock",
                    sock_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # 4. Give shim time to connect and start listening
            await asyncio.sleep(0.5)

            # 5. Connect via TCP and send test data
            r, w = await asyncio.open_connection("127.0.0.1", shim_port)

            payload = b"hello-shim-roundtrip!"
            w.write(payload)
            await w.drain()

            echoed = await asyncio.wait_for(r.readexactly(len(payload)), timeout=5)
            assert echoed == payload, f"Expected {payload!r}, got {echoed!r}"

            w.close()
            echo_server.close()
            await echo_server.wait_closed()

        try:
            asyncio.run(run())
        finally:
            if proc_shim is not None:
                _stop_proc(proc_shim)
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass


class TestShimExitCode2:
    """Shim exits with code 2 when the proxy socket never appears."""

    def test_exit_code_2_when_sock_missing(self):
        """Exit code 2 after 5 s retry timeout with a non-existent socket."""
        sock_path = os.path.join(_get_tmpdir(), f"nonexistent_{os.getpid()}.sock")
        shim_port = _free_port()

        # Ensure the socket doesn't exist
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pocketverse.shim",
                "--port",
                str(shim_port),
                "--sock",
                sock_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for exit (shim retries for 5 s, give it a bit longer)
        try:
            ret = proc.wait(timeout=7)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
            pytest.fail("shim did not exit within 7 s")

        assert ret == 2, f"Expected exit code 2, got {ret}"


# ===================================================================
# Cleanup
# ===================================================================


def pytest_unconfigure(config):  # noqa: ARG001
    global TMPDIR
    if TMPDIR is not None:
        import shutil

        shutil.rmtree(TMPDIR, ignore_errors=True)
        TMPDIR = None
