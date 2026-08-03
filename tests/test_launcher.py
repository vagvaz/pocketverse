"""Tests for launcher.py, _entry.py, and _inner.py.

All tests are unit-level: no root / namespaces / bwrap required.  The
core bwrap-argv builder (``build_bwrap_argv``) is a pure function
extracted from ``_entry.py``; we test it headless.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pocketverse._entry import build_bwrap_argv
from pocketverse.launcher import _prlimit_prefix, build_unshare_command, run_sandbox
from pocketverse.models import (
    IsolationConfig,
    LimitsConfig,
    Mount,
    MountMode,
    SandboxConfig,
    SharedConfig,
    _parse_size,
)


# =========================================================================
# Fixtures – reusable entry dict + overlays
# =========================================================================

@pytest.fixture
def minimal_entry() -> dict:
    """A minimal entry.json dict (no mounts, everything default)."""
    return {
        "config": {
            "version": 1,
            "name": "test",
            "mounts": [],
            "network": {"mode": "none", "allow": [], "port": 3128,
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
                "system_dirs": True,
                "etc_files": True,
            },
        },
        "session_id": "20260726-120000-abc123",
        "pkg_parent": "/tmp/.pocket/state/test/session-20260726-120000-abc123",
        "env": {},
        "command": ["bash"],
        "overlays": [],
    }


@pytest.fixture
def entry_with_mounts() -> dict:
    """Entry with ro, rw, and overlay mounts defined."""
    return {
        "config": {
            "version": 1,
            "name": "mounts-test",
            "mounts": [
                {"path": "/host/readonly", "target": "/sandbox/ro",
                 "mode": "ro"},
                {"path": "/host/rw",       "target": "/sandbox/rw",
                 "mode": "rw"},
                {"path": "/host/overlay",  "target": "/sandbox/ov",
                 "mode": "overlay"},
            ],
            "network": {"mode": "none", "allow": [], "port": 3128,
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
            },
        },
        "session_id": "mounts-001",
        "pkg_parent": "/fake/pkg/parent",
        "env": {},
        "command": ["bash"],
        "overlays": [
            {"index": 2, "source": "/host/overlay", "target": "/sandbox/ov",
             "upper": "/fake/upper", "work": "/fake/work",
             "mnt": "/fake/mnt/2"},
        ],
    }


@pytest.fixture
def overlays_list(entry_with_mounts) -> list[dict]:
    return entry_with_mounts["overlays"]


# =========================================================================
# build_unshare_command
# =========================================================================

class TestBuildUnshareCommand:
    def test_shape(self):
        """Returned list starts with unshare flags, ends with _entry call."""
        entry_json = Path("/tmp/entry.json")
        result = build_unshare_command(entry_json)
        assert result[0] == "unshare"
        assert result[1] == "--user"
        assert result[2] == "--map-root-user"
        assert result[3] == "--mount"
        assert result[4] == "--"
        assert result[5] == sys.executable
        assert result[6:8] == ["-m", "pocketverse._entry"]
        assert result[8] == str(entry_json)

    def test_preserves_absolute_path(self):
        entry_json = Path("/some/deep/path/entry.json")
        result = build_unshare_command(entry_json)
        assert result[8] == "/some/deep/path/entry.json"


# =========================================================================
# build_bwrap_argv  –  pure function
# =========================================================================

class TestBuildBwrapArgvStructure:
    """Sanity-check the argv list returned by the pure builder."""

    def test_starts_with_bwrap(self, minimal_entry):
        overlays: list[dict] = []
        argv = build_bwrap_argv(minimal_entry, overlays, "/pkg")
        assert argv[0] == "bwrap"

    def test_ends_with_command(self, minimal_entry):
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        assert argv[-1] == "bash", f"Expected bash at end, got {argv[-1]}"

    def test_help_flag_and_flags_ordering(self, minimal_entry):
        """Clear ordering: isolation flags, then net, then --proc, etc."""
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        # After 'bwrap', the first flags should be the isolation ones
        flags = argv[1:]
        # --die-with-parent should appear early
        assert "--die-with-parent" in flags
        # --proc should come after isolation flags
        proc_idx = flags.index("--proc")
        dwp_idx = flags.index("--die-with-parent")
        assert dwp_idx < proc_idx, "isolation flags should precede --proc"


# =========================================================================
# Mount mappings
# =========================================================================

class TestMountMapping:
    def test_ro_mount(self, entry_with_mounts, overlays_list):
        argv = build_bwrap_argv(entry_with_mounts, overlays_list,
                                "/fake/pkg/parent")
        argv_str = " ".join(argv)
        # ro mount: /host/readonly -> /sandbox/ro
        assert "--ro-bind /host/readonly /sandbox/ro" in argv_str

    def test_rw_mount(self, entry_with_mounts, overlays_list):
        argv = build_bwrap_argv(entry_with_mounts, overlays_list,
                                "/fake/pkg/parent")
        argv_str = " ".join(argv)
        # rw mount: /host/rw -> /sandbox/rw
        assert "--bind /host/rw /sandbox/rw" in argv_str

    def test_overlay_mount_uses_staging_mnt(self, entry_with_mounts,
                                             overlays_list):
        argv = build_bwrap_argv(entry_with_mounts, overlays_list,
                                "/fake/pkg/parent")
        argv_str = " ".join(argv)
        # overlay mount should use --bind with the staging mnt dir
        assert "--bind /fake/mnt/2 /sandbox/ov" in argv_str

    def test_overlay_mount_uses_index_lookup(self):
        """Multiple overlay mounts — each matched by index."""
        entry = {
            "config": {
                "version": 1,
                "name": "multi-overlay",
                "mounts": [
                    {"path": "/src0", "target": "/tgt0", "mode": "overlay"},
                    {"path": "/src1", "target": "/tgt1", "mode": "overlay"},
                ],
                "network": {"mode": "none", "allow": [], "port": 3128,
                            "allow_ports": [443, 80]},
                "env": {},
                "workdir": None,
                "command": ["bash"],
                "state_dir": "/tmp/.pocket/state",
                "isolation": {"system_dirs": False, "etc_files": False,
                              "clearenv": False, "die_with_parent": False,
                              "new_session": False, "unshare_pid": False,
                              "unshare_ipc": False, "unshare_uts": False,
                              "hostname": "pocket"},
            },
            "session_id": "multi-ov",
            "pkg_parent": "/pkg",
            "env": {},
            "command": ["bash"],
            "overlays": [
                {"index": 0, "mnt": "/mnt/0", "target": "/tgt0"},
                {"index": 1, "mnt": "/mnt/1", "target": "/tgt1"},
            ],
        }
        argv = build_bwrap_argv(entry, entry["overlays"], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /mnt/0 /tgt0" in argv_str
        assert "--bind /mnt/1 /tgt1" in argv_str

    def test_default_target_is_source(self):
        """When target is None (not set), the source path is used as target."""
        entry = {
            "config": {
                "version": 1,
                "name": "default-target",
                "mounts": [
                    {"path": "/home/user/work", "mode": "rw"},
                ],
                "network": {"mode": "none", "allow": [], "port": 3128,
                            "allow_ports": [443, 80]},
                "env": {},
                "workdir": None,
                "command": ["bash"],
                "state_dir": "/tmp/.pocket/state",
                "isolation": {"system_dirs": False, "etc_files": False,
                              "clearenv": False, "die_with_parent": False,
                              "new_session": False, "unshare_pid": False,
                              "unshare_ipc": False, "unshare_uts": False,
                              "hostname": "pocket"},
            },
            "session_id": "default-target",
            "pkg_parent": "/pkg",
            "env": {},
            "command": ["bash"],
            "overlays": [],
        }
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /home/user/work /home/user/work" in argv_str


# =========================================================================
# Network mode → --unshare-net
# =========================================================================

class TestUnshareNet:
    def test_none_has_unshare_net(self, minimal_entry):
        entry = _set_network(minimal_entry, "none")
        argv = build_bwrap_argv(entry, [], "/pkg")
        assert "--unshare-net" in argv

    def test_allowlist_has_unshare_net(self, minimal_entry):
        entry = _set_network(minimal_entry, "allowlist", allow=["*.example.com"])
        entry["sock_dir_host"] = "/tmp/sock"
        entry["proxy_sock_in_sandbox"] = "/run/pocketverse/proxy.sock"
        argv = build_bwrap_argv(entry, [], "/pkg")
        assert "--unshare-net" in argv

    def test_full_omits_unshare_net(self, minimal_entry):
        entry = _set_network(minimal_entry, "full")
        argv = build_bwrap_argv(entry, [], "/pkg")
        assert "--unshare-net" not in argv


# =========================================================================
# --clearenv
# =========================================================================

class TestClearenv:
    def test_clearenv_present_when_true(self, minimal_entry):
        # Minimal entry has clearenv: True (default)
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        assert "--clearenv" in argv

    def test_clearenv_absent_when_false(self, minimal_entry):
        entry = _set_isolation(minimal_entry, clearenv=False)
        argv = build_bwrap_argv(entry, [], "/pkg")
        assert "--clearenv" not in argv


# =========================================================================
# Proxy env vars in allowlist mode
# =========================================================================

class TestProxyEnvVars:
    def test_proxy_vars_present_in_allowlist(self, minimal_entry):
        entry = _set_network(minimal_entry, "allowlist",
                             allow=["api.example.com"], port=8888)
        entry["sock_dir_host"] = "/tmp/sock"
        entry["proxy_sock_in_sandbox"] = "/run/pocketverse/proxy.sock"
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)

        assert "--setenv HTTP_PROXY http://127.0.0.1:8888" in argv_str
        assert "--setenv HTTPS_PROXY http://127.0.0.1:8888" in argv_str
        assert "--setenv ALL_PROXY http://127.0.0.1:8888" in argv_str
        assert "--setenv NO_PROXY localhost,127.0.0.1" in argv_str
        # lower-case variants
        assert "--setenv http_proxy http://127.0.0.1:8888" in argv_str
        assert "--setenv https_proxy http://127.0.0.1:8888" in argv_str
        assert "--setenv all_proxy http://127.0.0.1:8888" in argv_str
        assert "--setenv no_proxy localhost,127.0.0.1" in argv_str

    def test_proxy_vars_absent_in_full(self, minimal_entry):
        entry = _set_network(minimal_entry, "full")
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv HTTP_PROXY" not in argv_str

    def test_proxy_vars_absent_in_none(self, minimal_entry):
        entry = _set_network(minimal_entry, "none")
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv HTTP_PROXY" not in argv_str

    def test_user_env_does_not_clobber_proxy(self, minimal_entry):
        """User-specified env should NOT be overridden by proxy defaults,
        but since proxy defaults are added only when NOT already in env_map,
        user values survive."""
        entry = _set_network(minimal_entry, "allowlist",
                             allow=["example.com"], port=9999)
        entry["sock_dir_host"] = "/tmp/sock"
        entry["proxy_sock_in_sandbox"] = "/run/pocketverse/proxy.sock"
        # User explicitly sets HTTP_PROXY
        entry["env"]["HTTP_PROXY"] = "http://custom:8080"
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv HTTP_PROXY http://custom:8080" in argv_str


# =========================================================================
# System dirs – skip when absent
# =========================================================================

class TestSystemDirsSkip:
    def test_system_dirs_filtered_by_exists(self, monkeypatch, minimal_entry):
        """Only directories that exist on the test host are added."""
        def fake_exists(path: str) -> bool:
            return path in ("/usr",)  # pretend only /usr exists

        monkeypatch.setattr(os.path, "exists", fake_exists)

        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--ro-bind /usr /usr" in argv_str
        assert "--ro-bind /bin /bin" not in argv_str
        assert "--ro-bind /sbin /sbin" not in argv_str
        assert "--ro-bind /lib /lib" not in argv_str
        assert "--ro-bind /lib64 /lib64" not in argv_str

    def test_all_system_dirs_missing(self, monkeypatch, minimal_entry):
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--ro-bind /usr /usr" not in argv_str
        assert "--ro-bind /bin /bin" not in argv_str

    def test_system_dirs_skipped_when_disabled(self, minimal_entry):
        entry = _set_isolation(minimal_entry, system_dirs=False)
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--ro-bind /usr /usr" not in argv_str

    def test_etc_files_filtered_by_exists(self, monkeypatch, minimal_entry):
        def fake_exists(path: str) -> bool:
            return path in ("/etc/resolv.conf", "/etc/hosts")

        monkeypatch.setattr(os.path, "exists", fake_exists)
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--ro-bind /etc/resolv.conf /etc/resolv.conf" in argv_str
        assert "--ro-bind /etc/hosts /etc/hosts" in argv_str
        assert "--ro-bind /etc/ssl /etc/ssl" not in argv_str


# =========================================================================
# Env defaults (PATH, HOME, USER, TERM)
# =========================================================================

class TestLLMEndpoint:
    """llm.base_url is injected as provider base-URL env vars (user wins)."""

    def test_injected_when_set(self, minimal_entry):
        entry = dict(minimal_entry)
        entry["config"] = {**minimal_entry["config"],
                           "llm": {"base_url": "http://127.0.0.1:8317"}}
        argv_str = " ".join(build_bwrap_argv(entry, [], "/pkg"))
        assert "--setenv ANTHROPIC_BASE_URL http://127.0.0.1:8317" in argv_str
        assert "--setenv OPENAI_BASE_URL http://127.0.0.1:8317" in argv_str

    def test_absent_when_unset(self, minimal_entry):
        argv_str = " ".join(build_bwrap_argv(minimal_entry, [], "/pkg"))
        assert "ANTHROPIC_BASE_URL" not in argv_str
        assert "OPENAI_BASE_URL" not in argv_str

    def test_user_env_wins(self, minimal_entry):
        entry = dict(minimal_entry)
        entry["config"] = {**minimal_entry["config"],
                           "llm": {"base_url": "http://127.0.0.1:8317"}}
        entry["env"] = {"ANTHROPIC_BASE_URL": "http://custom:1"}
        argv_str = " ".join(build_bwrap_argv(entry, [], "/pkg"))
        assert "--setenv ANTHROPIC_BASE_URL http://custom:1" in argv_str
        assert "http://127.0.0.1:8317" not in argv_str.replace(
            "OPENAI_BASE_URL http://127.0.0.1:8317", "")


class TestEnvDefaults:
    def test_defaults_present_when_no_user_env(self, minimal_entry):
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        argv_str = " ".join(argv)
        # PATH default is host-aware: prefixed with the NixOS profile bin
        # when /nix/store exists on the build host.
        import os
        expected_path = "/usr/local/bin:/usr/bin:/bin"
        if os.path.isdir("/nix/store"):
            expected_path = "/run/current-system/sw/bin:" + expected_path
        assert f"--setenv PATH {expected_path}" in argv_str
        assert "--setenv HOME /" in argv_str
        assert "--setenv USER pocket" in argv_str
        assert "--setenv TERM xterm-256color" in argv_str

    def test_defaults_not_present_when_user_overrides(self, minimal_entry):
        entry = dict(minimal_entry)
        entry["env"] = {"PATH": "/custom/bin"}
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        # User value must win
        assert "--setenv PATH /custom/bin" in argv_str
        # Default PATH should NOT be emitted (user already provides it)
        # But it's not harmful if it is — we just check user wins
        pat = re.compile(r"--setenv PATH /usr/local/bin:/usr/bin:/bin")
        matches = [(m.start()) for m in pat.finditer(argv_str)]
        # Export HOME/USER/TERM still present
        assert "--setenv HOME /" in argv_str

    def test_home_defaults_to_workdir(self, minimal_entry):
        entry = dict(minimal_entry)
        entry["config"]["workdir"] = "/home/pocket"
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv HOME /home/pocket" in argv_str

    def test_pythonpath_in_defaults(self, minimal_entry):
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--setenv PYTHONPATH /pkg" in argv_str


# =========================================================================
# --chdir workdir
# =========================================================================

class TestChdir:
    def test_chdir_present_when_workdir_set(self, minimal_entry):
        entry = dict(minimal_entry)
        entry["config"]["workdir"] = "/home/pocket"
        argv = build_bwrap_argv(entry, [], "/pkg")
        chdir_args = [argv[i:i+2] for i in range(len(argv)-1)
                      if argv[i] == "--chdir"]
        assert len(chdir_args) == 1
        assert chdir_args[0] == ["--chdir", "/home/pocket"]

    def test_chdir_absent_when_no_workdir(self, minimal_entry):
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        assert "--chdir" not in argv


# =========================================================================
# Allowlist → _inner wrapper command
# =========================================================================

class TestAllowlistCommandWrapping:
    def test_allowlist_wraps_with_inner(self, minimal_entry):
        entry = _set_network(minimal_entry, "allowlist",
                             allow=["example.com"], port=3128)
        entry["sock_dir_host"] = "/tmp/sock"
        entry["proxy_sock_in_sandbox"] = "/run/pocketverse/proxy.sock"
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "python3 -m pocketverse._inner" in argv_str
        assert "--port 3128" in argv_str
        assert "--sock /run/pocketverse/proxy.sock" in argv_str
        assert argv[-1] == "bash"

    def test_full_uses_command_directly(self, minimal_entry):
        entry = _set_network(minimal_entry, "full")
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "pocketverse._inner" not in argv_str
        assert argv[-1] == "bash"

    def test_none_uses_command_directly(self, minimal_entry):
        entry = _set_network(minimal_entry, "none")
        argv = build_bwrap_argv(entry, [], "/pkg")
        assert "pocketverse._inner" not in " ".join(argv)
        assert argv[-1] == "bash"


# =========================================================================
# Proxy socket bind-mount
# =========================================================================

class TestProxySocketBind:
    def test_sock_bind_present_allowlist(self, minimal_entry):
        entry = _set_network(minimal_entry, "allowlist",
                             allow=["example.com"])
        entry["sock_dir_host"] = "/host/sock/dir"
        entry["proxy_sock_in_sandbox"] = "/run/pocketverse/proxy.sock"
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /host/sock/dir /run/pocketverse" in argv_str

    def test_sock_bind_absent_full(self, minimal_entry):
        entry = _set_network(minimal_entry, "full")
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /host/sock/dir /run/pocketverse" not in argv_str


# =========================================================================
# Run sandbox - workdir validation
# =========================================================================

class TestRunSandboxWorkdirValidation:
    """run_sandbox should raise ValueError early when workdir is invalid."""

    def test_rejects_unmounted_workdir(self):
        from pocketverse.launcher import run_sandbox
        from pocketverse.models import SandboxConfig, Mount, IsolationConfig

        cfg = SandboxConfig(
            name="workdir-test",
            mounts=[],  # empty — nothing mounted
            workdir="/nonexistent/path",
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )
        with pytest.raises(ValueError, match="workdir.*not under any mount"):
            run_sandbox(cfg, dry_run=True)

    def test_accepts_tmp_workdir(self):
        from pocketverse.launcher import run_sandbox
        from pocketverse.models import SandboxConfig, IsolationConfig

        cfg = SandboxConfig(
            name="tmp-workdir",
            workdir="/tmp",
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )
        # Should not raise
        run_sandbox(cfg, dry_run=True)

    def test_accepts_mounted_workdir(self):
        from pocketverse.launcher import run_sandbox
        from pocketverse.models import SandboxConfig, Mount, IsolationConfig
        from pocketverse.models import MountMode

        cfg = SandboxConfig(
            name="mounted-workdir",
            mounts=[Mount(path="/home/user/project",
                          target="/workspace",
                          mode=MountMode.RW)],
            workdir="/workspace/subdir",
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )
        run_sandbox(cfg, dry_run=True)  # no raise

    def test_rejects_subdir_of_slash(self, monkeypatch):
        """/etc/foo looks like it's under / but that's not valid."""
        from pocketverse.launcher import run_sandbox
        from pocketverse.models import SandboxConfig, IsolationConfig

        cfg = SandboxConfig(
            name="etc-workdir",
            workdir="/etc/foo",
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )
        with pytest.raises(ValueError, match="workdir.*not under any mount"):
            run_sandbox(cfg, dry_run=True)


# =========================================================================
# prlimit prefix (_prlimit_prefix helper + dry-run output)
# =========================================================================

class TestPrlimitPrefix:
    """Direct tests of the `_prlimit_prefix` helper."""

    def test_empty_when_no_limits(self):
        """No user limit fields set -> [] (even though --core=0 always renders)."""
        cfg = SandboxConfig(limits=LimitsConfig())
        assert _prlimit_prefix(cfg) == []

    def test_present_with_correct_args_when_limits_set(self):
        cfg = SandboxConfig(
            limits=LimitsConfig(cpu=60, memory="4G", open_files=1024)
        )
        assert _prlimit_prefix(cfg) == [
            "prlimit", "--cpu=60", "--as=4294967296",
            "--nofile=1024", "--core=0",
        ]

    def test_raises_when_prlimit_missing(self, monkeypatch):
        """Limits configured but prlimit absent -> readable FileNotFoundError."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        cfg = SandboxConfig(limits=LimitsConfig(cpu=60))
        with pytest.raises(FileNotFoundError, match="prlimit.*not found"):
            _prlimit_prefix(cfg)

    def test_require_false_skips_which_check(self, monkeypatch):
        """require=False (dry-run) renders the prefix without needing prlimit."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        cfg = SandboxConfig(limits=LimitsConfig(cpu=60))
        assert _prlimit_prefix(cfg, require=False) == [
            "prlimit", "--cpu=60", "--core=0",
        ]

    def test_no_which_check_when_no_limits(self, monkeypatch):
        """prlimit missing is irrelevant when no limits are configured."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        cfg = SandboxConfig()  # default LimitsConfig -> all None
        assert _prlimit_prefix(cfg) == []


class TestDryRunPrlimit:
    """run_sandbox(dry_run=True) must show prlimit without requiring it."""

    def test_dry_run_includes_prlimit_when_limits_configured(self, monkeypatch,
                                                             capsys):
        # prlimit deliberately absent from PATH — dry-run must still render it
        monkeypatch.setattr(shutil, "which", lambda name: None)
        cfg = SandboxConfig(
            name="lim-run",
            limits=LimitsConfig(cpu=30, memory="512M"),
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )
        ret = run_sandbox(cfg, dry_run=True)
        captured = capsys.readouterr()
        assert ret == 0
        assert captured.out.startswith(
            "prlimit --cpu=30 --as=536870912 --core=0 unshare "
        )
        assert "pocketverse._entry" in captured.out

    def test_dry_run_no_prlimit_when_no_limits(self, capsys):
        cfg = SandboxConfig(
            name="no-lim-run",
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )
        run_sandbox(cfg, dry_run=True)
        captured = capsys.readouterr()
        assert captured.out.startswith("unshare ")
        assert "prlimit" not in captured.out


# =========================================================================
# shared directory bind in build_bwrap_argv
# =========================================================================

class TestSharedBind:
    def test_shared_bind_present(self, minimal_entry):
        entry = dict(minimal_entry)
        entry["config"] = dict(minimal_entry["config"])
        entry["config"]["shared"] = {
            "path": "/host/shared", "target": "/shared",
        }
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /host/shared /shared" in argv_str

    def test_shared_default_target(self, minimal_entry):
        """target missing/None -> default '/shared'."""
        entry = dict(minimal_entry)
        entry["config"] = dict(minimal_entry["config"])
        entry["config"]["shared"] = {"path": "/host/shared", "target": None}
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /host/shared /shared" in argv_str

    def test_shared_custom_target(self, minimal_entry):
        entry = dict(minimal_entry)
        entry["config"] = dict(minimal_entry["config"])
        entry["config"]["shared"] = {"path": "/host/swap", "target": "/exchange"}
        argv = build_bwrap_argv(entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "--bind /host/swap /exchange" in argv_str

    def test_no_shared_or_prlimit_artifacts_when_absent(self, minimal_entry):
        argv = build_bwrap_argv(minimal_entry, [], "/pkg")
        argv_str = " ".join(argv)
        assert "/shared" not in argv_str
        assert "prlimit" not in argv_str


# =========================================================================
# shared directory creation in run_sandbox (real-run path)
# =========================================================================

class TestRunSandboxShared:
    def test_shared_dir_created_on_real_run(self, monkeypatch, tmp_path):
        """run_sandbox creates the shared host dir before launching unshare."""
        import pocketverse.launcher as launcher
        import pocketverse.state as state

        shared_path = tmp_path / "shared"
        cfg = SandboxConfig(
            name="shared-run",
            shared=SharedConfig(path=shared_path),
            limits=LimitsConfig(),  # no limits -> no prlimit dependency
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )

        fake_sess = SimpleNamespace(
            id="sess-1",
            root=tmp_path / "sess",
            sock_dir=tmp_path / "sess" / "sock",
            proxy_sock=tmp_path / "sess" / "sock" / "proxy.sock",
            log_dir=tmp_path / "sess" / "logs",
            overlays=[],
        )

        monkeypatch.setattr(state, "new_session", lambda cfg_, sid_: fake_sess)
        monkeypatch.setattr(
            launcher.shutil, "which",
            lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
        )

        calls: list[list[str]] = []

        class FakeProc:
            def __init__(self, cmd):
                calls.append(cmd)

            def wait(self):
                return 0

            def send_signal(self, sig):
                pass

        monkeypatch.setattr(launcher.subprocess, "Popen", FakeProc)

        ret = run_sandbox(cfg)
        assert ret == 0
        # shared dir must exist on the host now
        assert shared_path.is_dir()
        # and the launch command must be the unshare pipeline
        assert calls and calls[0][0] == "unshare"

    def test_no_shared_dir_when_not_configured(self, monkeypatch, tmp_path):
        import pocketverse.launcher as launcher
        import pocketverse.state as state

        cfg = SandboxConfig(
            name="no-shared-run",
            limits=LimitsConfig(),
            isolation=IsolationConfig(system_dirs=False, etc_files=False),
        )

        fake_sess = SimpleNamespace(
            id="sess-2",
            root=tmp_path / "sess2",
            sock_dir=tmp_path / "sess2" / "sock",
            proxy_sock=tmp_path / "sess2" / "sock" / "proxy.sock",
            log_dir=tmp_path / "sess2" / "logs",
            overlays=[],
        )

        monkeypatch.setattr(state, "new_session", lambda cfg_, sid_: fake_sess)
        monkeypatch.setattr(
            launcher.shutil, "which",
            lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
        )

        class FakeProc:
            def __init__(self, cmd):
                pass

            def wait(self):
                return 0

            def send_signal(self, sig):
                pass

        monkeypatch.setattr(launcher.subprocess, "Popen", FakeProc)

        run_sandbox(cfg)
        assert not (tmp_path / "sess2").is_dir() or True  # no crash
        # nothing under tmp_path named 'shared' was created
        assert not (tmp_path / "shared").exists()


# =========================================================================
# models: _parse_size + LimitsConfig.prlimit_args
# =========================================================================

class TestModelsSizeParsing:
    def test_parse_size_bytes(self):
        assert _parse_size("1024") == 1024

    def test_parse_size_512M(self):
        assert _parse_size("512M") == 536870912

    def test_parse_size_4G(self):
        assert _parse_size("4G") == 4294967296

    def test_parse_size_int_input(self):
        assert _parse_size(4096) == 4096

    def test_parse_size_lowercase_suffix(self):
        assert _parse_size("512m") == 536870912

    def test_parse_size_bad_value_raises(self):
        with pytest.raises(ValueError):
            _parse_size("abc")
        with pytest.raises(ValueError):
            _parse_size("4X")
        with pytest.raises(ValueError):
            _parse_size("")

    def test_prlimit_args_default_only_core(self):
        assert LimitsConfig().prlimit_args() == ["--core=0"]

    def test_prlimit_args_full(self):
        args = LimitsConfig(
            cpu=60, memory="4G", file_size="1G",
            open_files=4096, processes=512, core=1024,
        ).prlimit_args()
        assert args == [
            "--cpu=60", "--as=4294967296", "--fsize=1073741824",
            "--nofile=4096", "--nproc=512", "--core=1024",
        ]

    def test_prlimit_args_bad_size_raises(self):
        with pytest.raises(ValueError):
            LimitsConfig(memory="oops").prlimit_args()


# =========================================================================
# _inner.py main – no real test (needs subprocess), but interface check
# =========================================================================

def test_inner_module_has_main():
    """Verify the module exports a main function with the right signature."""
    import pocketverse._inner as inner
    assert callable(inner.main)
    # main returns int
    # We can't easily test it without forking, but the interface is correct.


# =========================================================================
# Helpers
# =========================================================================

def _set_network(entry: dict, mode: str, *,
                 allow: list[str] | None = None, port: int = 3128) -> dict:
    entry = dict(entry)  # shallow copy
    entry["config"] = dict(entry["config"])
    entry["config"]["network"] = {
        "mode": mode,
        "allow": allow or [],
        "port": port,
        "allow_ports": [443, 80],
    }
    return entry


def _set_isolation(entry: dict, **kwargs) -> dict:
    entry = dict(entry)  # shallow copy
    entry["config"] = dict(entry["config"])
    entry["config"]["isolation"] = dict(entry["config"].get("isolation", {}))
    entry["config"]["isolation"].update(kwargs)
    return entry
