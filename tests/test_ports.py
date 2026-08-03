"""Tests for port forwarding: allocation, unishim relay, and bwrap argv.

The unishim + chain tests exercise the real asyncio relay code over local
sockets/TCP on 127.0.0.1 — no root, namespaces, or bwrap required.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

from pocketverse._entry import build_bwrap_argv
from pocketverse.launcher import _allocate_ports
from pocketverse.models import IsolationConfig, PortForward, SandboxConfig


# =========================================================================
# Helpers
# =========================================================================

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _base_entry(*, net_mode: str = "none", ports: list[dict] | None = None,
                sock_dir_host: str | None = None) -> dict:
    """Minimal entry.json dict with optional top-level ports allocation."""
    entry = {
        "config": {
            "version": 1,
            "name": "ports-test",
            "mounts": [],
            "shared": None,
            "ports": None,
            "network": {"mode": net_mode, "allow": [], "port": 3128,
                        "allow_ports": [443, 80]},
            "env": {},
            "workdir": None,
            "command": ["bash"],
            "state_dir": "/tmp/.pocket/state",
            "isolation": {
                "die_with_parent": True,
                "new_session": True,
                "unshare_pid": True,
                "unshare_ipc": True,
                "unshare_uts": True,
                "hostname": "pocket",
                "clearenv": True,
                "system_dirs": False,
                "etc_files": False,
                "bind_nix_store": False,
            },
        },
        "session_id": "ports-001",
        "pkg_parent": "/fake/pkg/parent",
        "env": {},
        "command": ["bash"],
        "overlays": [],
    }
    if sock_dir_host is not None:
        entry["sock_dir_host"] = sock_dir_host
    if net_mode == "allowlist":
        entry["proxy_sock_in_sandbox"] = "/run/pocketverse/proxy.sock"
    if ports is not None:
        entry["ports"] = ports  # top-level allocation list (as launcher writes)
    return entry


# =========================================================================
# _allocate_ports
# =========================================================================

class TestAllocatePorts:
    def test_allocates_bindable_free_ports(self):
        """Auto-allocated host ports must be distinct and immediately bindable."""
        cfg = SandboxConfig(ports=[PortForward(target=3000),
                                   PortForward(target=3001)])
        allocated = _allocate_ports(cfg)
        assert [p["target"] for p in allocated] == [3000, 3001]
        hosts = [p["host"] for p in allocated]
        assert len(set(hosts)) == 2, "auto-allocated ports must be distinct"
        for h in hosts:
            assert 1 <= h <= 65535
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", h))
            finally:
                s.close()

    def test_honors_fixed_host(self):
        cfg = SandboxConfig(ports=[PortForward(target=8080, host=18080)])
        assert _allocate_ports(cfg) == [
            {"name": None, "target": 8080, "host": 18080},
        ]

    def test_mixed_fixed_and_auto(self):
        cfg = SandboxConfig(ports=[PortForward(target=8080, host=18080),
                                   PortForward(target=9000)])
        allocated = _allocate_ports(cfg)
        by_target = {p["target"]: p for p in allocated}
        assert by_target[8080]["host"] == 18080
        assert by_target[9000]["host"] != 18080
        assert 1 <= by_target[9000]["host"] <= 65535

    def test_duplicate_targets_rejected(self):
        cfg = SandboxConfig(ports=[PortForward(target=3000),
                                   PortForward(target=3000)])
        with pytest.raises(ValueError, match="duplicate"):
            _allocate_ports(cfg)


# =========================================================================
# unishim relay roundtrip: unix socket -> TCP echo server
# =========================================================================

class TestUnishimRoundtrip:
    def test_bytes_both_ways(self, tmp_path):
        """A unix-socket client reaches a TCP echo server via unishim."""
        sock_path = tmp_path / "fwd.sock"
        target = _free_port()

        async def run():
            # --- in-test TCP echo server (what the agent would run) --------
            async def echo(reader, writer):
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
                writer.close()

            echo_server = await asyncio.start_server(
                echo, "127.0.0.1", target
            )

            # --- unishim subprocess (in-sandbox relay) ---------------------
            uni = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pocketverse.unishim",
                "--sock", str(sock_path),
                "--target", str(target),
            )

            # wait for the unix socket to appear
            for _ in range(50):
                if sock_path.exists():
                    break
                await asyncio.sleep(0.1)
            assert sock_path.exists(), "unishim did not create its socket"

            # --- unix-socket client ----------------------------------------
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(b"hello from the unix side")
            await writer.drain()
            echoed = await asyncio.wait_for(reader.read(1024), timeout=5)
            assert echoed == b"hello from the unix side"

            writer.close()
            await writer.wait_closed()

            uni.terminate()
            await uni.wait()
            echo_server.close()
            await echo_server.wait_closed()

        asyncio.run(run())

    def test_creates_parent_dir(self, tmp_path):
        """unishim mkdir -ps the socket parent before binding."""
        sock_path = tmp_path / "deep" / "nested" / "fwd.sock"
        target = _free_port()

        async def run():
            uni = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pocketverse.unishim",
                "--sock", str(sock_path),
                "--target", str(target),
            )
            for _ in range(50):
                if sock_path.exists():
                    break
                await asyncio.sleep(0.1)
            assert sock_path.exists()
            assert sock_path.parent.is_dir()
            uni.terminate()
            await uni.wait()

        asyncio.run(run())

    def test_unlinks_stale_socket(self, tmp_path):
        """A stale socket left by a previous run must not block the bind."""
        sock_path = tmp_path / "stale.sock"
        # create a stale (dead) unix socket file
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.close()
        assert sock_path.exists()

        target = _free_port()

        async def run():
            uni = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pocketverse.unishim",
                "--sock", str(sock_path),
                "--target", str(target),
            )
            # unishim should have unlinked the stale socket and bound fresh
            for _ in range(50):
                try:
                    r, w = await asyncio.open_unix_connection(str(sock_path))
                    w.close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            uni.terminate()
            await uni.wait()

        asyncio.run(run())


# =========================================================================
# Full chain: TCP client -> host shim -> unix socket -> unishim -> TCP echo
# =========================================================================

class TestShimUnishimChain:
    def test_relay_pair_composes(self, tmp_path):
        """Host TCP clients reach the sandbox TCP server through both relays."""
        sock_path = tmp_path / "fwd.sock"
        target = _free_port()
        host_port = _free_port()

        async def run():
            # --- sandbox-side TCP echo server -----------------------------
            async def echo(reader, writer):
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
                writer.close()

            echo_server = await asyncio.start_server(
                echo, "127.0.0.1", target
            )

            # --- in-sandbox unishim (unix listener -> TCP target) ----------
            uni = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pocketverse.unishim",
                "--sock", str(sock_path),
                "--target", str(target),
            )
            for _ in range(50):
                if sock_path.exists():
                    break
                await asyncio.sleep(0.1)
            assert sock_path.exists()

            # --- host-side shim (TCP listener -> unix socket) --------------
            shim = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pocketverse.shim",
                "--port", str(host_port),
                "--sock", str(sock_path),
                "--bind", "127.0.0.1",
            )
            # shim retries the socket itself, but wait for it to accept
            for _ in range(60):
                try:
                    r, w = await asyncio.open_connection(
                        "127.0.0.1", host_port
                    )
                    w.close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("host shim never started listening")

            # --- host TCP client roundtrip ---------------------------------
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", host_port
            )
            writer.write(b"ping through the relay pair")
            await writer.drain()
            echoed = await asyncio.wait_for(reader.read(1024), timeout=5)
            assert echoed == b"ping through the relay pair"

            writer.close()
            await writer.wait_closed()

            shim.terminate()
            await shim.wait()
            uni.terminate()
            await uni.wait()
            echo_server.close()
            await echo_server.wait_closed()

        asyncio.run(run())


# =========================================================================
# build_bwrap_argv: port-forward artifacts
# =========================================================================

class TestBwrapArgvPorts:
    def test_env_injection_pocket_port_web(self):
        entry = _base_entry(
            net_mode="none",
            ports=[{"name": "web", "target": 3000, "host": 12345}],
            sock_dir_host="/host/sock",
        )
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv POCKET_PORT_WEB 12345" in argv_str

    def test_env_injection_uppercases_name(self):
        entry = _base_entry(
            net_mode="none",
            ports=[{"name": "api_server", "target": 8000, "host": 9999}],
            sock_dir_host="/host/sock",
        )
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv POCKET_PORT_API_SERVER 9999" in argv_str

    def test_user_env_wins_over_port_env(self):
        entry = _base_entry(
            net_mode="none",
            ports=[{"name": "web", "target": 3000, "host": 12345}],
            sock_dir_host="/host/sock",
        )
        entry["env"] = {"POCKET_PORT_WEB": "7777"}
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv POCKET_PORT_WEB 7777" in argv_str

    def test_sock_bind_present_in_none_mode_when_ports(self):
        entry = _base_entry(
            net_mode="none",
            ports=[{"name": "web", "target": 3000, "host": 12345}],
            sock_dir_host="/host/sock",
        )
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /host/sock /run/pocketverse" in argv_str

    def test_sock_bind_present_in_full_mode_when_ports(self):
        entry = _base_entry(
            net_mode="full",
            ports=[{"name": "web", "target": 3000, "host": 12345}],
            sock_dir_host="/host/sock",
        )
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /host/sock /run/pocketverse" in argv_str

    def test_inner_wrapper_used_in_full_mode_when_ports(self):
        """Even without --unshare-net, ports need the _inner wrapper."""
        entry = _base_entry(
            net_mode="full",
            ports=[{"target": 3000, "host": 12345}],
            sock_dir_host="/host/sock",
        )
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "python3 -m pocketverse._inner" in argv_str
        assert "--ports-json /run/pocketverse/ports.json" in argv_str
        assert argv[-1] == "bash"

    def test_allowlist_with_ports_has_both_args(self):
        entry = _base_entry(
            net_mode="allowlist",
            ports=[{"name": "web", "target": 3000, "host": 12345}],
            sock_dir_host="/host/sock",
        )
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--port 3128" in argv_str
        assert "--sock /run/pocketverse/proxy.sock" in argv_str
        assert "--ports-json /run/pocketverse/ports.json" in argv_str

    def test_no_port_artifacts_when_absent(self):
        entry = _base_entry(net_mode="none")
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "/run/pocketverse" not in argv_str
        assert "POCKET_PORT" not in argv_str
        assert "pocketverse._inner" not in argv_str
        assert argv[-1] == "bash"


# =========================================================================
# entry.json content: launcher writes the ports list through
# =========================================================================

class TestEntryJsonPorts:
    def test_entry_json_contains_ports(self, monkeypatch, tmp_path):
        """run_sandbox writes the allocation into entry.json and ports.json."""
        import pocketverse.launcher as launcher
        import pocketverse.state as state

        root = tmp_path / "sess"
        sock_dir = root / "sock"
        root.mkdir(parents=True)
        sock_dir.mkdir(parents=True)
        fake_sess = type("FakeSession", (), {
            "id": "ports-sess",
            "root": root,
            "sock_dir": sock_dir,
            "proxy_sock": sock_dir / "proxy.sock",
            "log_dir": root / "logs",
            "overlays": [],
        })()

        cfg = SandboxConfig(
            name="ports-run",
            ports=[PortForward(target=3000, name="web")],
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )
        # pin a fixed host so we can assert on it deterministically
        cfg.ports[0].host = 12345

        monkeypatch.setattr(state, "new_session", lambda cfg_, sid_: fake_sess)
        monkeypatch.setattr(
            launcher.shutil, "which",
            lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
        )

        calls: list[list[str]] = []

        class FakeProc:
            def __init__(self, cmd):
                calls.append(cmd)

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

            def send_signal(self, sig):
                pass

        monkeypatch.setattr(launcher.subprocess, "Popen", FakeProc)

        ret = launcher.run_sandbox(cfg)
        assert ret == 0

        # entry.json carries the ports list
        entry = json.loads((root / "entry.json").read_text())
        assert entry["ports"] == [
            {"name": "web", "target": 3000, "host": 12345},
        ]
        assert entry["sock_dir_host"] == str(sock_dir)

        # ports.json (CLI copy) carries the same list + session id
        ports_doc = json.loads((root / "ports.json").read_text())
        assert ports_doc["session_id"] == "ports-sess"
        assert ports_doc["ports"] == entry["ports"]

        # a host shim was spawned per forward
        shim_calls = [c for c in calls if "-m" in c and "pocketverse.shim" in c]
        assert len(shim_calls) == 1
        shim_str = " ".join(shim_calls[0])
        assert "--port 12345" in shim_str
        assert str(sock_dir / "fwd-3000.sock") in shim_str
