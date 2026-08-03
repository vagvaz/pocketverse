"""Agent runtime adapters: command construction, native session ids, state.

When config `agent.type` is not `shell`, pockets host a real agent CLI
instead of a plain command. This module is the single place that knows
each runtime's flags (verified against official docs, 2026-08):

  claude-code: headless `claude -p <goal> --output-format json`
               native id CHOSEN by us via `--session-id <uuid>`
               resume `claude -p <goal> --resume <id>` (same conversation)
               native spend cap `--max-budget-usd`
               interactive `claude [--session-id <uuid> | --resume <id>]`
               state: $HOME/.claude
  opencode:    headless `opencode run <goal> --format json`
               resume `opencode run <goal> --session <id>` (continue)
               ids are captured from json events (best effort)
               state: $HOME/.local/share/opencode

Agent pockets get a stable HOME (/home/pocket) with the state dirs
bind-mounted to persistent per-config locations, so native resume works
ACROSS pockets of the same config name.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .models import AgentType, Mount, MountMode, SandboxConfig

_log = logging.getLogger(__name__)

AGENT_HOME = "/home/pocket"

_CLAUDE_STATE = ".claude"
_OPENCODE_STATE = ".local/share/opencode"


def state_mounts(cfg: SandboxConfig) -> list[Mount]:
    """Persistent rw state mounts for the configured agent runtime.

    Rooted at <state_dir>/.agent-state/<config.name>/ so every pocket of
    the same config shares the agent's session database.
    """
    base = cfg.state_dir / ".agent-state" / cfg.name
    if cfg.agent.type is AgentType.CLAUDE:
        return [Mount(path=base / "claude",
                      target=Path(AGENT_HOME) / _CLAUDE_STATE,
                      mode=MountMode.RW)]
    if cfg.agent.type is AgentType.OPENCODE:
        return [Mount(path=base / "opencode",
                      target=Path(AGENT_HOME) / _OPENCODE_STATE,
                      mode=MountMode.RW)]
    return []


def build_command(
    cfg: SandboxConfig,
    goal: str | None,
    *,
    session_id: str | None,
    resume_native_id: str | None,
    interactive: bool,
) -> list[str] | None:
    """Return the agent CLI argv, or None when type == shell (use config.command).

    session_id: orchestrator-chosen native id (claude only; opencode ids
    are server-assigned and captured after the run).
    resume_native_id: native session id to continue (claude: same
    conversation via --resume; opencode: --session). Resume takes
    precedence over session_id (the continued conversation keeps its id).
    """
    at = cfg.agent.type
    if at is AgentType.SHELL:
        return None

    if at is AgentType.CLAUDE:
        budget = (["--max-budget-usd", str(cfg.agent.max_budget_usd)]
                  if cfg.agent.max_budget_usd is not None else [])
        if interactive:
            if resume_native_id:
                return ["claude", "--resume", resume_native_id, *budget]
            return ["claude", *(["--session-id", session_id] if session_id else []),
                    *budget]
        argv = ["claude", "-p", goal or "", "--output-format", "json"]
        if resume_native_id:
            argv += ["--resume", resume_native_id]
        elif session_id:
            argv += ["--session-id", session_id]
        return argv + budget

    if at is AgentType.OPENCODE:
        if interactive:
            return ["opencode", *(["--session", resume_native_id]
                                   if resume_native_id else [])]
        argv = ["opencode", "run", goal or "", "--format", "json"]
        if resume_native_id:
            argv += ["--session", resume_native_id]
        return argv

    raise ValueError(f"unknown agent type: {at}")


_SESSION_PATTERNS = [
    re.compile(r'"session_id"\s*:\s*"([^"]+)"'),   # claude json output
    re.compile(r'"sessionID"\s*:\s*"([^"]+)"'),    # opencode json events
    re.compile(r'"sessionId"\s*:\s*"([^"]+)"'),
]


def capture_native_id(agent_type: AgentType, log_path: Path) -> str | None:
    """Best-effort native session id extraction from a child's output log."""
    try:
        text = Path(log_path).read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        for pat in _SESSION_PATTERNS:
            m = pat.search(line)
            if m:
                return m.group(1)
    # last resort: any json object anywhere (some runtimes pretty-print)
    for pat in _SESSION_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    _log.warning("no native session id found in %s", log_path)
    return None
