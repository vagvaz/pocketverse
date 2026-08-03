"""Tests for pocketverse.supervisor — spawn loop over mailbox requests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pocketverse import mailbox
from pocketverse.models import AgentConfig, AgentType, SandboxConfig, SharedConfig
from pocketverse.supervisor import _Supervisor, run_supervisor


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.rc: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.rc

    def terminate(self) -> None:
        self.terminated = True
        self.rc = -15

    def kill(self) -> None:
        self.rc = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.rc


class FakePopen:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.procs: list[FakeProc] = []

    def __call__(self, argv, **kwargs):
        proc = FakeProc(1000 + len(self.procs))
        self.procs.append(proc)
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return proc


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    return tmp_path / "shared"


def make_cfg(shared: Path, **sup_kwargs) -> SandboxConfig:
    sup = {"poll_interval": 0.05, "max_concurrent": 4, "max_fanout_per_parent": 3,
           **sup_kwargs}
    return SandboxConfig(
        name="test",
        shared=SharedConfig(path=shared),
        state_dir="/tmp/pv-test-state",
        supervisor=sup,
    )


def send_spawn(shared: Path, sender: str, goal: str, **extra) -> str:
    payload = {"sender": sender, "goal": goal, **extra}
    return mailbox.append(shared, "supervisor", "spawn_request", payload, sender).id


def read_manifest(shared: Path, sid: str) -> dict:
    return json.loads((shared / "sessions" / f"{sid}.json").read_text())


def manifests(shared: Path) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text())
            for p in (shared / "sessions").glob("*.json")}


def inbox(shared: Path, who: str) -> list:
    msgs, _ = mailbox.read(shared, who)
    return msgs


class TestBasics:
    def test_missing_shared_raises(self) -> None:
        with pytest.raises(ValueError, match="shared"):
            run_supervisor(SandboxConfig(name="x"), once=True)

    def test_single_tick_launches_and_cursors(self, shared: Path) -> None:
        cfg = make_cfg(shared)
        send_spawn(shared, "agent-a", "write the tests")
        popen = FakePopen()
        assert run_supervisor(cfg, once=True, _popen=popen) == 0
        assert len(popen.calls) == 1
        argv = popen.calls[0]["argv"]
        assert argv[1:4] == ["-m", "pocketverse.cli", "run"]
        sid = argv[-1]
        # child config has env overrides
        child = yaml.safe_load((shared / "sessions" / f"{sid}.yaml").read_text())
        assert child["env"]["POCKET_GOAL"] == "write the tests"
        assert child["env"]["POCKET_SESSION_ID"] == sid
        assert child["env"]["POCKET_SENDER"] == "agent-a"
        assert (shared / "goals" / f"{sid}.md").read_text() == "write the tests"
        m = read_manifest(shared, sid)
        assert m["status"] == "running" and m["pid"] == 1000
        # second tick: cursor consumed, no relaunch
        run_supervisor(cfg, once=True, _popen=popen)
        assert len(popen.calls) == 1

    def test_invalid_payload_notifies_error(self, shared: Path) -> None:
        cfg = make_cfg(shared)
        mailbox.append(shared, "supervisor", "spawn_request",
                       {"bogus": 1}, "agent-b")
        run_supervisor(cfg, once=True, _popen=FakePopen())
        errors = [m for m in inbox(shared, "agent-b") if m.type == "error"]
        assert len(errors) == 1
        assert "in_reply_to" in errors[0].payload


class TestDeps:
    def test_dep_waits_then_launches(self, shared: Path) -> None:
        cfg = make_cfg(shared)
        send_spawn(shared, "agent-a", "summarize", depends_on=["dep-1"])
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        assert len(popen.calls) == 0  # dep manifest missing -> waiting
        (shared / "sessions").mkdir(parents=True, exist_ok=True)
        (shared / "sessions" / "dep-1.json").write_text(json.dumps(
            {"id": "dep-1", "status": "done"}))
        sup.tick()
        assert len(popen.calls) == 1

    def test_failed_dep_fails_request_once(self, shared: Path) -> None:
        cfg = make_cfg(shared)
        send_spawn(shared, "agent-a", "summarize", depends_on=["dep-x"])
        (shared / "sessions").mkdir(parents=True, exist_ok=True)
        (shared / "sessions" / "dep-x.json").write_text(json.dumps(
            {"id": "dep-x", "status": "failed"}))
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        sup.tick()
        assert len(popen.calls) == 0
        failed = [m for m in inbox(shared, "agent-a") if m.type == "failed"]
        assert len(failed) == 1
        assert failed[0].payload["reason"] == "dependency failed"


class TestCaps:
    def test_fanout_cap_notifies_once(self, shared: Path) -> None:
        cfg = make_cfg(shared, max_fanout_per_parent=1)
        send_spawn(shared, "agent-a", "first")
        send_spawn(shared, "agent-a", "second")
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        sup.tick()
        assert len(popen.calls) == 1  # only 'first'
        failed = [m for m in inbox(shared, "agent-a") if m.type == "failed"]
        assert len(failed) == 1
        assert failed[0].payload["reason"] == "fanout cap reached"

    def test_max_concurrent_then_reap_then_launch(self, shared: Path) -> None:
        cfg = make_cfg(shared, max_concurrent=1)
        send_spawn(shared, "agent-a", "one")
        send_spawn(shared, "agent-a", "two")
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        assert len(popen.calls) == 1
        sid1 = popen.calls[0]["argv"][-1]
        # first exits OK -> reap marks done + notifies parent
        popen.procs[0].rc = 0
        sup.tick()
        m1 = read_manifest(shared, sid1)
        assert m1["status"] == "done" and m1["exit_code"] == 0
        dones = [m for m in inbox(shared, "agent-a") if m.type == "done"]
        assert len(dones) == 1 and dones[0].payload["session"] == sid1
        # slot freed -> second launches
        assert len(popen.calls) == 2

    def test_reap_failure(self, shared: Path) -> None:
        cfg = make_cfg(shared)
        send_spawn(shared, "agent-a", "boom")
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        sid = popen.calls[0]["argv"][-1]
        popen.procs[0].rc = 3
        sup.tick()
        m = read_manifest(shared, sid)
        assert m["status"] == "failed" and m["exit_code"] == 3
        failed = [x for x in inbox(shared, "agent-a") if x.type == "failed"]
        assert len(failed) == 1 and failed[0].payload["exit_code"] == 3


class TestAgentChildren:
    def make_agent_cfg(self, shared: Path, atype: AgentType, **kw) -> SandboxConfig:
        cfg = make_cfg(shared, **kw)
        cfg.agent = AgentConfig(type=atype, max_budget_usd=5.0)
        return cfg

    def test_claude_child_command_and_manifest(self, shared: Path) -> None:
        cfg = self.make_agent_cfg(shared, AgentType.CLAUDE)
        send_spawn(shared, "agent-a", "fix the bug")
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        (call,) = popen.calls
        argv = call["argv"]
        sid = argv[argv.index("--session") + 1]
        assert argv[argv.index("--") + 1:][:5] == [
            "claude", "-p", "fix the bug", "--output-format", "json"]
        assert "--session-id" in argv
        assert argv[-2:] == ["--max-budget-usd", "5.0"]
        m = read_manifest(shared, sid)
        assert m["agent_type"] == "claude-code"
        assert m["agent_session_id"]  # orchestrator-chosen uuid

    def test_continue_from_resolves_native_id(self, shared: Path) -> None:
        cfg = self.make_agent_cfg(shared, AgentType.CLAUDE)
        (shared / "sessions").mkdir(parents=True, exist_ok=True)
        (shared / "sessions" / "parent-1.json").write_text(json.dumps(
            {"id": "parent-1", "status": "done",
             "agent_type": "claude-code", "agent_session_id": "parent-uuid"}))
        send_spawn(shared, "agent-a", "summarize",
                   depends_on=["parent-1"], continue_from="parent-1")
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        (call,) = popen.calls
        argv = call["argv"]
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == "parent-uuid"
        # resumed conversation keeps the parent id; no fresh --session-id
        assert "--session-id" not in argv[argv.index("--") + 1:]

    def test_opencode_reap_captures_native_id(self, shared: Path) -> None:
        cfg = self.make_agent_cfg(shared, AgentType.OPENCODE)
        send_spawn(shared, "agent-a", "run it")
        popen = FakePopen()
        sup = _Supervisor(cfg, popen)
        sup.tick()
        argv = popen.calls[0]["argv"]
        sid = argv[argv.index("--session") + 1]
        assert argv[argv.index("--") + 1:][:3] == ["opencode", "run", "run it"]
        # simulate the agent's json event log
        (shared / "logs" / f"{sid}.log").write_text(
            '{"type":"session","sessionID":"ses_oc99"}\n')
        popen.procs[0].rc = 0
        sup.tick()
        m = read_manifest(shared, sid)
        assert m["status"] == "done"
        assert m["agent_session_id"] == "ses_oc99"
