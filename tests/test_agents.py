"""Tests for pocketverse.agents — runtime adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketverse import agents
from pocketverse.models import AgentConfig, AgentType, SandboxConfig


def cfg_with(agent_type: AgentType, budget: float | None = None) -> SandboxConfig:
    return SandboxConfig(
        name="t",
        state_dir="/tmp/pv-agent-test",
        agent=AgentConfig(type=agent_type, max_budget_usd=budget),
    )


class TestBuildCommandClaude:
    def test_headless_fresh(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.CLAUDE), "fix the bug",
            session_id="uuid-1", resume_native_id=None, interactive=False)
        assert cmd == ["claude", "-p", "fix the bug", "--output-format", "json",
                       "--session-id", "uuid-1"]

    def test_headless_resume_no_new_session_id(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.CLAUDE), "continue",
            session_id="uuid-2", resume_native_id="rid-1", interactive=False)
        assert cmd == ["claude", "-p", "continue", "--output-format", "json",
                       "--resume", "rid-1"]

    def test_budget_flag(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.CLAUDE, budget=5.0), "g",
            session_id="u", resume_native_id=None, interactive=False)
        assert cmd[-2:] == ["--max-budget-usd", "5.0"]

    def test_interactive_fresh(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.CLAUDE), None,
            session_id="uuid-3", resume_native_id=None, interactive=True)
        assert cmd == ["claude", "--session-id", "uuid-3"]

    def test_interactive_resume(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.CLAUDE), None,
            session_id=None, resume_native_id="rid-9", interactive=True)
        assert cmd == ["claude", "--resume", "rid-9"]


class TestBuildCommandOpencode:
    def test_headless_fresh(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.OPENCODE), "write tests",
            session_id=None, resume_native_id=None, interactive=False)
        assert cmd == ["opencode", "run", "write tests", "--format", "json"]

    def test_headless_continue(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.OPENCODE), "more",
            session_id=None, resume_native_id="ses_abc", interactive=False)
        assert cmd == ["opencode", "run", "more", "--format", "json",
                       "--session", "ses_abc"]

    def test_interactive(self) -> None:
        cmd = agents.build_command(
            cfg_with(AgentType.OPENCODE), None,
            session_id=None, resume_native_id="ses_x", interactive=True)
        assert cmd == ["opencode", "--session", "ses_x"]


class TestShellAndMounts:
    def test_shell_returns_none(self) -> None:
        assert agents.build_command(
            cfg_with(AgentType.SHELL), "x",
            session_id=None, resume_native_id=None, interactive=False) is None
        assert agents.state_mounts(cfg_with(AgentType.SHELL)) == []

    def test_claude_state_mount(self) -> None:
        (m,) = agents.state_mounts(cfg_with(AgentType.CLAUDE))
        assert m.path == Path("/tmp/pv-agent-test/.agent-state/t/claude")
        assert m.target == Path("/home/pocket/.claude")
        assert m.mode.value == "rw"

    def test_opencode_state_mount(self) -> None:
        (m,) = agents.state_mounts(cfg_with(AgentType.OPENCODE))
        assert m.target == Path("/home/pocket/.local/share/opencode")


class TestCaptureNativeId:
    def test_claude_json(self, tmp_path: Path) -> None:
        log = tmp_path / "l.log"
        log.write_text('noise\n{"type":"result","session_id":"claude-123","x":1}\n')
        assert agents.capture_native_id(AgentType.CLAUDE, log) == "claude-123"

    def test_opencode_event(self, tmp_path: Path) -> None:
        log = tmp_path / "l.log"
        log.write_text('{"type":"session","sessionID":"ses_oc42"}\n{}\n')
        assert agents.capture_native_id(AgentType.OPENCODE, log) == "ses_oc42"

    def test_missing(self, tmp_path: Path) -> None:
        log = tmp_path / "l.log"
        log.write_text("no ids here\n")
        assert agents.capture_native_id(AgentType.CLAUDE, log) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert agents.capture_native_id(
            AgentType.CLAUDE, tmp_path / "nope.log") is None
