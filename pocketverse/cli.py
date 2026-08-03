"""pocket CLI — launch, diff, apply, and manage pocketverse sandboxes.

Usage::

    pocket run      -c CONFIG [--session ID] [--dry-run] [-- CMD...]
    pocket diff     -c CONFIG [--session ID|latest]
    pocket apply    -c CONFIG [--session ID|latest] [--no-backup] [--dry-run]
    pocket sessions -c CONFIG
    pocket validate -c CONFIG
    pocket init     [PATH] [--force]
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from . import launcher
from .models import MountMode, load_config
from .state import (
    Change,
    apply_session,
    diff_session,
    list_sessions,
    load_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_session_id() -> str:
    """Produce a unique session id: ``<UTC %Y%m%d-%H%M%S>-<6 lowercase hex>``."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_hex(3)
    return f"{ts}-{rand}"


def _print_changes(changes: list[Change]) -> None:
    """Print changes in the standard CLI format."""
    for c in changes:
        detail = f"  ({c.detail})" if c.detail else ""
        print(f"{c.kind.value:8} [mount {c.mount_index}] {c.relpath}{detail}")
    if changes:
        plural = "s" if len(changes) != 1 else ""
        print(f"---\nsummary: {len(changes)} change{plural}")
    else:
        print("no changes")


def _load_config_or_exit(config_path: Path):
    """Load config. On error, print message to stderr and exit 2."""
    try:
        return load_config(config_path)
    except FileNotFoundError:
        print(f"error: config not found: {config_path}", file=sys.stderr)
        sys.exit(2)
    except yaml.YAMLError as e:
        print(f"error: invalid YAML in {config_path}: {e}", file=sys.stderr)
        sys.exit(2)
    except ValidationError as e:
        _print_validation_error(e)
        sys.exit(2)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


def _print_validation_error(e: ValidationError) -> None:
    """Print a pydantic ValidationError readably."""
    print(f"validation error:", file=sys.stderr)
    for err in e.errors():
        loc = " -> ".join(str(p) for p in err["loc"])
        print(f"  {loc}: {err['msg']}", file=sys.stderr)


def _load_session_or_exit(cfg, session_id: str):
    """Load session. On error, print to stderr and exit 2."""
    try:
        return load_session(cfg, session_id)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)

    # Generate or use explicit session id
    session_id = args.session or _generate_session_id()
    print(f"session: {session_id}", file=sys.stderr)

    # Strip leading '--' from command remainder if present
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        cmd = None

    try:
        return launcher.run_sandbox(
            cfg,
            session_id=session_id,
            cmd=cmd,
            dry_run=args.dry_run,
            continue_from=args.continue_from,
        )
    except (FileNotFoundError, ValidationError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def cmd_diff(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)
    session = _load_session_or_exit(cfg, args.session)

    try:
        changes = diff_session(session)
    except Exception as e:
        print(f"error: diff failed: {e}", file=sys.stderr)
        return 2

    _print_changes(changes)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)
    session = _load_session_or_exit(cfg, args.session)

    result = apply_session(
        session,
        backup=not args.no_backup,
        dry_run=args.dry_run,
    )

    _print_changes(result.applied)

    if result.backup_dir:
        print(f"backup: {result.backup_dir}")

    if result.errors:
        for err in result.errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)

    for sid in list_sessions(cfg):
        print(sid)
    return 0


def cmd_ports(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)
    session = _load_session_or_exit(cfg, args.session)

    ports_json = session.root / "ports.json"
    if not ports_json.exists():
        print("no port forwards recorded for this session", file=sys.stderr)
        return 0

    try:
        data = json.loads(ports_json.read_text())
    except ValueError as e:
        print(f"error: cannot parse {ports_json}: {e}", file=sys.stderr)
        return 2

    ports = data.get("ports", []) if isinstance(data, dict) else []
    if not ports:
        print("no port forwards recorded for this session", file=sys.stderr)
        return 0

    print(f"{'NAME':<16} {'HOST':<24} TARGET")
    for p in ports:
        name = p.get("name") or "-"
        host = f"127.0.0.1:{p['host']}"
        print(f"{name:<16} {host:<24} {p['target']}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)

    # Build summary
    mount_counts: dict[str, int] = {}
    for m in cfg.mounts:
        mount_counts[m.mode.value] = mount_counts.get(m.mode.value, 0) + 1
    mount_desc = ", ".join(
        f"{v} {k}" for k, v in mount_counts.items()
    )
    net_mode = cfg.network.mode.value if cfg.network else "none"
    allow_count = len(cfg.network.allow) if cfg.network else 0
    env_count = len(cfg.env)

    print(f"name: {cfg.name}")
    print(f"mounts: {len(cfg.mounts)} ({mount_desc})")
    if allow_count:
        print(f"network: {net_mode} ({allow_count} rules)")
    else:
        print(f"network: {net_mode}")
    print(f"variables: {env_count} env var{'s' if env_count != 1 else ''}")
    print("config OK")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force to overwrite)", file=sys.stderr)
        return 2

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_EXAMPLE_CONFIG)
    print(f"wrote {path}")
    return 0


def _shared_root_or_exit(cfg) -> Path:
    if cfg.shared is None:
        print("error: this command needs 'shared:' configured in the config",
              file=sys.stderr)
        sys.exit(2)
    return cfg.shared.path


def cmd_supervisor(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)
    _shared_root_or_exit(cfg)
    from .supervisor import run_supervisor
    try:
        return run_supervisor(cfg, once=args.once)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _print_message(m) -> None:
    print(f"[{m.ts}] {m.type} from={m.sender} id={m.id}")
    if m.payload:
        print(json.dumps(m.payload, indent=2, sort_keys=True))


def cmd_mailbox(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)
    shared = _shared_root_or_exit(cfg)
    from .mailbox import follow, read
    if args.tail:
        try:
            for m in follow(shared, args.recipient, offset=args.offset):
                _print_message(m)
        except KeyboardInterrupt:
            return 0
        return 0
    messages, new_offset = read(shared, args.recipient, offset=args.offset)
    for m in messages:
        _print_message(m)
    if args.print_offset:
        print(f"offset: {new_offset}", file=sys.stderr)
    return 0


def cmd_mailbox_send(args: argparse.Namespace) -> int:
    cfg = _load_config_or_exit(args.config)
    shared = _shared_root_or_exit(cfg)
    from .mailbox import append
    if args.payload_file:
        raw = Path(args.payload_file).read_text()
        payload = yaml.safe_load(raw)
        if not isinstance(payload, dict):
            print("error: --payload-file must contain a mapping", file=sys.stderr)
            return 2
    elif args.payload:
        try:
            payload = json.loads(args.payload)
        except ValueError as e:
            print(f"error: invalid --payload JSON: {e}", file=sys.stderr)
            return 2
    elif args.text is not None:
        payload = {"text": args.text}
    else:
        payload = {}
    if "sender" not in payload and args.type == "spawn_request":
        payload["sender"] = args.sender
    msg = append(shared, args.recipient, args.type, payload, args.sender)
    print(f"sent {msg.id}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Example config (also written to examples/pocketverse.yaml)
# ---------------------------------------------------------------------------

_EXAMPLE_CONFIG = """\
# pocketverse.yaml — annotated example configuration for an AI coding agent
# ===========================================================================
# pocket is the CLI entry point.  Run `pocket init` to create this file.
# Then edit it to match your environment, and launch the sandbox with:
#
#     pocket run [-c pocketverse.yaml] [--session custom-id] [-- CMD...]
#     pocket diff ...
#     pocket apply ...
#
# See `pocket validate` to check the config before running.

# Schema version (currently 1).
version: 1

# Human-readable name used as a subdirectory under state_dir.
name: ai-agent

# ---------------------------------------------------------------------------
# Mounts — host directories composed into the sandbox filesystem.
# Each entry has:
#   path   - host path (tilded/ENV-var expanded)
#   target - path inside the sandbox (defaults to same as path)
#   mode   - ro | rw | overlay
#
# ro:       read-only bind mount (kernel-enforced)
# rw:       read-write bind mount (changes hit the host immediately)
# overlay:  copy-on-write via overlayfs + userxattr (kernel >= 5.11).
#           Writes land in the session upper dir and can be diffed/applied.
# ---------------------------------------------------------------------------
mounts:
  # Workspace directory as an OVERLAY so all changes are tracked.
  - path: /home/user/code/my-project
    target: /workspace
    mode: overlay

  # A reference checkout read-only.
  - path: /home/user/repos/some-lib
    target: /reference
    mode: ro

# ---------------------------------------------------------------------------
# Network
#   mode: full | allowlist | none
#     full:      share host network (no isolation - bypass the proxy).
#     allowlist: only the domain-allowlist forward proxy is reachable.
#                The sandbox runs in --unshare-net; the proxy socket is
#                bind-mounted in and an in-sandbox shim exposes it as
#                127.0.0.1:3128.  HTTP_PROXY/HTTPS_PROXY are set automatically.
#     none:      fresh network namespace with loopback only.
# ---------------------------------------------------------------------------
network:
  mode: allowlist
  # Domain rules for allowlist mode:
  allow:
    - "api.anthropic.com"
    # - "api.openai.com"
    # - "*.github.com"
    # - "pypi.org:443"
  # Port the in-sandbox relay listens on (default 3128).
  # port: 3128
  # Upstream ports the proxy may connect to (unless a rule pins one).
  # allow_ports: [443, 80]

# ---------------------------------------------------------------------------
# Environment variables.
# Values undergo shell-style expansion on the host, so a value of
# "${ANTHROPIC_API_KEY}" passes the host environment variable through.
# ---------------------------------------------------------------------------
env:
  ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
  # OPENAI_API_KEY:   "${OPENAI_API_KEY}"

# Working directory inside the sandbox (must be under a mount target or /tmp).
workdir: /workspace

# Default command (can be overridden on the CLI with `pocket run -- <cmd>`).
command: ["bash"]

# Directory for session state (overlay uppers, proxy socket, metadata).
# Created relative to CWD if relative.
state_dir: .pocket/state

# ---------------------------------------------------------------------------
# Isolation tuning (defaults shown)
# ---------------------------------------------------------------------------
# isolation:
#   die_with_parent: true
#   new_session: true
#   unshare_pid: true
#   unshare_ipc: true
#   unshare_uts: true
#   hostname: pocket
#   clearenv: true          # bwrap --clearenv (empty env inside sandbox)
#   system_dirs: true       # ro-bind /usr /bin /sbin /lib /lib64
#   etc_files: true         # ro-bind /etc/resolv.conf, hosts, ssl, ca-certificates
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pocket",
        description="Composable unprivileged sandboxes for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    p_run = sub.add_parser("run", help="Run a sandbox session")
    p_run.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
        help="Config file path (default: pocketverse.yaml)",
    )
    p_run.add_argument(
        "--session", default=None,
        help="Explicit session id (auto-generated if omitted)",
    )
    p_run.add_argument(
        "--dry-run", action="store_true",
        help="Print the would-be unshare command and exit",
    )
    p_run.add_argument(
        "--continue-from", default=None,
        help="Session id to continue (worktree branch advance / overlay chain)",
    )
    p_run.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="Command and arguments (separate with --)",
    )
    p_run.set_defaults(func=cmd_run)

    # --- diff ---
    p_diff = sub.add_parser("diff", help="Show changes from a session")
    p_diff.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_diff.add_argument(
        "--session", default="latest",
        help="Session id or 'latest' (default: latest)",
    )
    p_diff.set_defaults(func=cmd_diff)

    # --- apply ---
    p_apply = sub.add_parser("apply", help="Apply session changes back to source")
    p_apply.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_apply.add_argument(
        "--session", default="latest",
    )
    p_apply.add_argument(
        "--no-backup", action="store_true",
        help="Skip timestamped backup before applying",
    )
    p_apply.add_argument(
        "--dry-run", action="store_true",
        help="Show the diff without applying",
    )
    p_apply.set_defaults(func=cmd_apply)

    # --- sessions ---
    p_sess = sub.add_parser("sessions", help="List all sessions")
    p_sess.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_sess.set_defaults(func=cmd_sessions)

    # --- ports ---
    p_ports = sub.add_parser(
        "ports", help="Show published port forwards for a session",
    )
    p_ports.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_ports.add_argument(
        "--session", default="latest",
        help="Session id or 'latest' (default: latest)",
    )
    p_ports.set_defaults(func=cmd_ports)

    # --- validate ---
    p_val = sub.add_parser("validate", help="Validate a config file")
    p_val.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_val.set_defaults(func=cmd_validate)

    # --- init ---
    p_init = sub.add_parser("init", help="Write an annotated example config")
    p_init.add_argument(
        "path", nargs="?", default="pocketverse.yaml", type=Path,
        help="Output path (default: pocketverse.yaml)",
    )
    p_init.add_argument(
        "--force", action="store_true",
        help="Overwrite existing file",
    )
    p_init.set_defaults(func=cmd_init)

    # --- supervisor ---
    p_sup = sub.add_parser(
        "supervisor", help="Run the spawn supervisor loop (needs shared:)",
    )
    p_sup.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_sup.add_argument(
        "--once", action="store_true",
        help="Run exactly one tick and exit (for tests/debug)",
    )
    p_sup.set_defaults(func=cmd_supervisor)

    # --- mailbox ---
    p_mb = sub.add_parser(
        "mailbox", help="Read a mailbox on the shared dir (needs shared:)",
    )
    p_mb.add_argument("recipient", help="Mailbox name (e.g. human, supervisor, a session id)")
    p_mb.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_mb.add_argument(
        "--offset", type=int, default=0,
        help="Byte offset to read from (cursor; default 0)",
    )
    p_mb.add_argument(
        "--tail", action="store_true", help="Follow new messages (Ctrl-C to stop)",
    )
    p_mb.add_argument(
        "--print-offset", action="store_true",
        help="Print the new cursor offset to stderr after reading",
    )
    p_mb.set_defaults(func=cmd_mailbox)

    # --- mailbox-send ---
    p_ms = sub.add_parser(
        "mailbox-send", help="Append a message to a mailbox (needs shared:)",
    )
    p_ms.add_argument("recipient", help="Mailbox name")
    p_ms.add_argument(
        "-c", "--config", default="pocketverse.yaml", type=Path,
    )
    p_ms.add_argument("--type", required=True,
                      help="Message type (spawn_request|question|done|...)")
    p_ms.add_argument("--payload", help="Inline JSON object")
    p_ms.add_argument("--payload-file", type=Path,
                      help="YAML/JSON file with the payload mapping")
    p_ms.add_argument("--text", help="Shorthand for payload {'text': ...}")
    p_ms.add_argument("--from", dest="sender", default="human",
                      help="Sender name (default: human)")
    p_ms.set_defaults(func=cmd_mailbox_send)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Accepts argv for testability; reads sys.argv when None."""
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse calls sys.exit(2) on error; propagate cleanly
        return e.code if isinstance(e.code, int) else 2

    try:
        return args.func(args)
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, KeyboardInterrupt):
            return 130
        return e.code if isinstance(e.code, int) else 2


if __name__ == "__main__":
    raise SystemExit(main())
