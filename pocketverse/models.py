"""Configuration contract for pocketverse sandboxes.

This module is the single source of truth for the YAML schema. It is
written by the architect and treated as read-only by implementation
lanes; if a lane needs a contract change it must report back instead of
editing this file.
"""

from __future__ import annotations

import enum
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class MountMode(str, enum.Enum):
    """How a host directory is exposed inside the sandbox."""

    RO = "ro"            # read-only bind mount
    RW = "rw"            # direct read-write bind (changes hit the host immediately)
    OVERLAY = "overlay"  # copy-on-write via overlayfs; changes land in the session upper dir


class NetMode(str, enum.Enum):
    """Network isolation tier."""

    FULL = "full"            # sandbox shares host network, unrestricted
    ALLOWLIST = "allowlist"  # no direct egress; only the domain-allowlist proxy is reachable
    NONE = "none"            # no network at all (fresh netns, loopback only)


class WorktreeConfig(BaseModel):
    """Git worktree integration for an OVERLAY mount.

    On run, pocketverse creates `git worktree add` on a per-session branch
    and points the overlay's lower at the worktree instead of the original
    directory. The repo's real git dir is rw-bound into the sandbox so the
    agent can commit; its commits land in the real repo's object store.

    Review = `git log/diff session-<id>`; merge (the worktree-mode
    replacement for `pocket apply`) = `git merge session-<id>`. Overlay
    diff/apply still works and targets the WORKTREE (for uncommitted
    leftovers at exit). Handoff: a continuation session branches from this
    session's branch tip.
    """

    repo: Path | None = None
    """Git repository path; defaults to the mount's `path`."""

    branch: str = "auto"
    """'auto' = session-<session-id>; or an explicit branch name. An
    existing explicit branch is checked out as-is (no -b)."""


class Mount(BaseModel):
    """One directory composed into the sandbox filesystem."""

    path: Path
    """Host directory to expose. `~` and environment variables are expanded at load time."""

    target: Path | None = None
    """Path inside the sandbox. Defaults to the same absolute path as `path`."""

    mode: MountMode = MountMode.RO

    worktree: WorktreeConfig | None = None
    """Overlay mounts only: back the overlay with a per-session git worktree."""

    @model_validator(mode="after")
    def _worktree_requires_overlay(self) -> "Mount":
        if self.worktree is not None and self.mode is not MountMode.OVERLAY:
            raise ValueError(
                "worktree is only valid on overlay mounts "
                f"(mount {self.path}, mode={self.mode.value})")
        return self

    @property
    def resolved_target(self) -> Path:
        return self.target if self.target is not None else self.path


class NetworkConfig(BaseModel):
    mode: NetMode = NetMode.NONE

    allow: list[str] = Field(default_factory=list)
    """Domain rules for ALLOWLIST mode: 'example.com', '*.example.com', or with an
    explicit port 'example.com:8443'. A bare domain also matches nothing but itself
    (no implicit subdomain match)."""

    port: int = 3128
    """Port the in-sandbox relay listens on (127.0.0.1 inside the sandbox)."""

    allow_ports: list[int] = Field(default_factory=lambda: [443, 80])
    """Upstream ports the proxy may connect to, unless a rule pins an explicit port."""

    @field_validator("allow")
    @classmethod
    def _validate_rules(cls, rules: list[str]) -> list[str]:
        for r in rules:
            if not re.fullmatch(r"(\*\.)?[A-Za-z0-9._-]+(:[0-9]{1,5})?", r):
                raise ValueError(f"invalid allowlist rule: {r!r}")
        return rules


class IsolationConfig(BaseModel):
    die_with_parent: bool = True
    """Kill the sandbox if the launcher process dies."""

    new_session: bool = True
    """setsid() inside the sandbox (blocks TIOCSTI terminal injection)."""

    unshare_pid: bool = True
    unshare_ipc: bool = True
    unshare_uts: bool = True
    hostname: str = "pocket"
    clearenv: bool = True
    """Start the sandbox with an empty environment (bwrap --clearenv)."""

    system_dirs: bool = True
    """Read-only bind /usr, /bin, /sbin, /lib, /lib64 when present on the host."""

    bind_nix_store: bool = True
    """Read-only bind /nix/store when present (NixOS hosts have no /usr or /bin;
    the store is the userland). Also puts /run/current-system/sw/bin first in
    the default sandbox PATH. Harmless on non-Nix hosts (skipped when absent)."""

    etc_files: bool = True
    """Read-only bind TLS/DNS config from /etc (resolv.conf, hosts, ssl, ca-certificates)."""


class SharedConfig(BaseModel):
    """A directory shared live across concurrent sandboxes.

    Changes are reflected on the host IMMEDIATELY (plain rw bind, no
    overlay, nothing for `pocket apply` to merge). Intended for
    inter-agent exchange, not for gated work — use overlay mounts for that.
    The directory is created on the host at launch if missing.
    """

    path: Path
    target: Path = Path("/shared")


_SIZE_SUFFIXES = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _parse_size(value: str) -> int:
    """Parse '536870912', '512M', '4G' (base 1024) into bytes."""
    s = str(value).strip().upper()
    mult = 1
    if s and s[-1] in _SIZE_SUFFIXES:
        mult = _SIZE_SUFFIXES[s[-1]]
        s = s[:-1]
    if not s.isdigit():
        raise ValueError(f"invalid size: {value!r} (use bytes or K/M/G/T suffix)")
    return int(s) * mult


class LimitsConfig(BaseModel):
    """Resource limits applied to the whole sandbox via prlimit(1).

    RLIMITs are inherited across fork/exec, so wrapping the outer `unshare`
    caps everything inside (bwrap, shim, agent). All fields optional.
    """

    cpu: int | None = None
    """RLIMIT_CPU — total CPU seconds before SIGXCPU."""

    memory: str | None = None
    """RLIMIT_AS — address space per process, e.g. '4G', '512M'."""

    file_size: str | None = None
    """RLIMIT_FSIZE — max created file size, e.g. '1G'."""

    open_files: int | None = None
    """RLIMIT_NOFILE — max open file descriptors."""

    processes: int | None = None
    """RLIMIT_NPROC — CAVEAT: counted against your real uid across the whole
    desktop, not just the sandbox; too-low values break forking entirely."""

    core: int = 0
    """RLIMIT_CORE — core dump size in bytes (0 disables dumps)."""

    def prlimit_args(self) -> list[str]:
        """Render as prlimit(1) arguments, e.g. ['--cpu=60', '--as=4294967296'].

        Sizes accept plain ints (bytes) or K/M/G/T suffixes (base 1024).
        Raises ValueError on malformed sizes.
        """
        args: list[str] = []
        if self.cpu is not None:
            args.append(f"--cpu={self.cpu}")
        if self.memory is not None:
            args.append(f"--as={_parse_size(self.memory)}")
        if self.file_size is not None:
            args.append(f"--fsize={_parse_size(self.file_size)}")
        if self.open_files is not None:
            args.append(f"--nofile={self.open_files}")
        if self.processes is not None:
            args.append(f"--nproc={self.processes}")
        args.append(f"--core={self.core}")
        return args


class PortForward(BaseModel):
    """Publish a sandbox TCP port on the host loopback.

    The agent inside listens on `target` (sandbox loopback — always
    collision-free in none/allowlist mode since the netns is private).
    pocketverse allocates a free host port (`host: null` = auto) and relays
    127.0.0.1:host -> unix socket -> 127.0.0.1:target inside the sandbox.
    Works in every network mode (unix sockets transcend netns); in `full`
    mode it is redundant but harmless. If `name` is set, the host port is
    injected into the sandbox env as POCKET_PORT_<NAME> (uppercased).
    """

    target: int
    host: int | None = None
    name: str | None = None

    @field_validator("target", "host")
    @classmethod
    def _valid_port(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError(f"port out of range: {v}")
        return v


class AgentType(str, enum.Enum):
    """Which agent runtime a pocket hosts."""

    SHELL = "shell"            # plain command (default; current behavior)
    CLAUDE = "claude-code"     # Anthropic Claude Code CLI
    OPENCODE = "opencode"      # opencode CLI


class AgentConfig(BaseModel):
    """Agent runtime integration.

    When type != shell, the launcher/supervisor builds the agent command
    (see agents.py), gives the pocket a stable HOME=/home/pocket, and
    bind-mounts persistent per-config state dirs so native session resume
    works ACROSS pockets:

        <state_dir>/.agent-state/<config.name>/claude   -> /home/pocket/.claude
        <state_dir>/.agent-state/<config.name>/opencode -> /home/pocket/.local/share/opencode

    Native session ids: claude sessions get an orchestrator-chosen UUID
    (--session-id); opencode ids are captured from --format json events
    (best effort). Both land in the session manifest as agent_session_id.
    """

    type: AgentType = AgentType.SHELL

    max_budget_usd: float | None = None
    """claude-code: passed as --max-budget-usd (native spend cap)."""


class SupervisorConfig(BaseModel):
    """Host-side spawn loop settings (`pocket supervisor`).

    The supervisor polls the 'supervisor' mailbox on the shared dir for
    spawn_request messages and launches pockets for them. Caps protect
    against spawn storms.
    """

    poll_interval: float = 2.0
    """Seconds between supervisor ticks."""

    max_concurrent: int = 4
    """Global cap on simultaneously running pockets."""

    max_fanout_per_parent: int = 3
    """Cap on children per requesting session (lifetime, not concurrent)."""


class SpawnRequest(BaseModel):
    """Payload of a mailbox message with type='spawn_request'.

    The supervisor validates mailbox payloads into this model, then
    launches a pocket whose env carries POCKET_GOAL / POCKET_SESSION_ID /
    POCKET_SENDER (+ POCKET_CONTINUE_FROM when set).
    """

    sender: str
    """Session id or agent name of the requester."""

    goal: str
    """Natural-language goal handed to the child agent."""

    depends_on: list[str] = Field(default_factory=list)
    """Session ids that must reach status 'done' before launch. A failed
    dependency fails the request (requester is notified)."""

    continue_from: str | None = None
    """Session id whose work this child continues (files via branch/overlay
    chain, intent via log tail — wired in build step 5)."""

    name: str | None = None
    """Human label; defaults to '<supervisor-name>-<short-session-id>'."""

    config: dict[str, Any] | str | None = None
    """None = inherit the supervisor's config; dict = inline SandboxConfig
    mapping; str = path to a pocketverse YAML."""


class MailboxMessage(BaseModel):
    """One line in a shared:/mailbox/<recipient>/in.jsonl file."""

    id: str = ""
    """Set by mailbox.append (hex uuid)."""

    sender: str
    ts: str = ""
    """ISO-8601, set by mailbox.append."""

    type: str
    """spawn_request | progress | question | answer | handoff | done |
    failed | error | ... (free-form, convention over enumeration)."""

    payload: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    """Optional LLM endpoint override injected into the sandbox environment.

    Mechanism only — pocketverse runs no proxy and does no metering. The
    point is to be ABLE to send background agents through a different base
    URL (e.g. a rate-limited lane in front of the provider) while humans
    keep using the provider default. When set, ANTHROPIC_BASE_URL and
    OPENAI_BASE_URL are injected unless already defined in `env:`. Remote
    endpoints also need their domain in `network.allow`; keys go through
    `env:` as usual.
    """

    base_url: str | None = None


class TelemetryConfig(BaseModel):
    """Optional telemetry settings (pocketverse.telemetry).

    The local JSONL layer (``<session.root>/events.jsonl``) always runs and
    needs no dependencies. Setting ``enabled: true`` additionally wires up
    OpenTelemetry traces/logs/metrics when the optional ``[telemetry]`` extra
    is installed; if the SDK is absent it is a silent no-op. OTLP exporters
    honour the standard ``OTEL_*`` environment variables.
    """

    enabled: bool = False
    """Wire up OpenTelemetry. Local JSONL runs regardless."""

    service_name: str = "pocketverse"
    """``service.name`` resource attribute; overridden by OTEL_SERVICE_NAME."""

    export_logs: bool = True
    """Mirror events to OpenTelemetry logs."""

    export_traces: bool = True
    """Emit OpenTelemetry spans (see telemetry.start_span)."""

    export_metrics: bool = False
    """Enable a meter provider with an OTLP metric reader."""


class SandboxConfig(BaseModel):
    version: int = 1
    name: str = "default"
    mounts: list[Mount] = Field(default_factory=list)
    shared: SharedConfig | None = None
    ports: list[PortForward] = Field(default_factory=list)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables. Values undergo shell-style expansion on the host,
    so '${ANTHROPIC_API_KEY}' passes the host value through."""

    workdir: str | None = None
    command: list[str] = Field(default_factory=lambda: ["bash"])
    state_dir: Path = Path(".pocket/state")
    """Root for session state (overlay uppers, proxy socket, metadata)."""

    isolation: IsolationConfig = Field(default_factory=IsolationConfig)

    @field_validator("network")
    @classmethod
    def _allowlist_needs_rules(cls, net: NetworkConfig) -> NetworkConfig:
        if net.mode is NetMode.ALLOWLIST and not net.allow:
            raise ValueError("network.mode=allowlist requires a non-empty 'allow' list")
        return net

    def overlay_mounts(self) -> list[Mount]:
        return [m for m in self.mounts if m.mode is MountMode.OVERLAY]

    def expanded_env(self) -> dict[str, str]:
        """Env map with $VAR/${VAR} expanded from the host environment."""
        return {k: os.path.expandvars(v) for k, v in self.env.items()}


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_config(path: str | Path) -> SandboxConfig:
    """Load and validate a YAML sandbox config.

    Expands `~` and environment variables in mount paths and state_dir.
    Raises pydantic.ValidationError / ValueError on bad configs.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    cfg = SandboxConfig.model_validate(raw)
    # Resolve paths to absolute, host-side.
    for m in cfg.mounts:
        m.path = Path(os.path.expandvars(str(m.path))).expanduser().resolve()
        if m.target is not None:
            m.target = Path(str(m.target))
    if cfg.shared is not None:
        cfg.shared.path = (
            Path(os.path.expandvars(str(cfg.shared.path))).expanduser().resolve()
        )
    cfg.state_dir = Path(os.path.expandvars(str(cfg.state_dir))).expanduser().resolve()
    for m in cfg.mounts:
        if m.worktree is not None and m.worktree.repo is not None:
            m.worktree.repo = (
                Path(os.path.expandvars(str(m.worktree.repo)))
                .expanduser().resolve()
            )
    for k in cfg.env:
        if not _ENV_NAME.match(k):
            raise ValueError(f"invalid env var name: {k!r}")
    return cfg
