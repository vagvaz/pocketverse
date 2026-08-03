"""Detached-session control built on a persistent tmux server.

The tmux socket lives in the session's bind-mounted socket directory. This
keeps the control channel outside the sandbox network namespace while the
commands themselves remain inside the bwrap namespaces.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .models import load_config
from .state import load_session


def _tmux_socket(session) -> Path:
    return session.sock_dir / "tmux.sock"


def session_running(cfg, session_id: str) -> bool:
    try:
        session = load_session(cfg, session_id)
        data = json.loads((session.root / "run.json").read_text())
        pid = int(data["pid"])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, KeyError, FileNotFoundError):
        return False


def _require_tmux() -> None:
    if subprocess.run(["tmux", "-V"], capture_output=True).returncode != 0:
        raise FileNotFoundError("tmux is required for detached sessions")


def serve(config_path: str | Path, session_id: str, command: list[str] | None) -> int:
    """Server process: owns the sandbox until the tmux session exits."""
    from .launcher import run_sandbox

    cfg = load_config(config_path)
    return run_sandbox(
        cfg,
        session_id=session_id,
        cmd=command,
        tmux_session=True,
    )


def run_detached(cfg, config_path: str | Path, session_id: str,
                 command: list[str] | None = None) -> int:
    """Start a server process and return immediately."""
    _require_tmux()
    log_dir = cfg.state_dir / cfg.name
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"detached-{session_id}.log"
    argv = [sys.executable, "-m", "pocketverse.sessionctl", "serve",
            "-c", str(config_path),
            "--session", session_id]
    if command:
        argv += ["--", *command]
    with log.open("ab") as stream:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return 0


def attach(cfg, session_id: str) -> int:
    _require_tmux()
    session = load_session(cfg, session_id)
    if not session_running(cfg, session_id):
        raise RuntimeError(f"session {session_id} is not running")
    return subprocess.run(["tmux", "-S", str(_tmux_socket(session)),
                           "attach-session", "-t", "pocket"]).returncode


def detach(cfg, session_id: str) -> int:
    _require_tmux()
    session = load_session(cfg, session_id)
    if not session_running(cfg, session_id):
        raise RuntimeError(f"session {session_id} is not running")
    result = subprocess.run(["tmux", "-S", str(_tmux_socket(session)),
                             "detach-client", "-a"],
                            capture_output=True, text=True)
    if result.returncode != 0 and "no current client" not in result.stderr.lower():
        return result.returncode
    return 0


def exec_command(cfg, session_id: str, command: list[str]) -> int:
    """Run a command in a detached session and return its exit status."""
    _require_tmux()
    if not command:
        raise ValueError("exec requires a command")
    session = load_session(cfg, session_id)
    if not session_running(cfg, session_id):
        raise RuntimeError(f"session {session_id} is not running")
    sock = str(_tmux_socket(session))
    subprocess.run(["tmux", "-S", sock, "set-option", "-t", "pocket",
                    "remain-on-exit", "on"], capture_output=True, check=False)
    token = secrets.token_hex(8)
    out_path = session.sock_dir / f"exec-{token}.out"
    status_path = session.sock_dir / f"exec-{token}.status"
    script = (
        f"\"$@\" > {shlex.quote('/run/pocketverse/' + out_path.name)} 2>&1; "
        f"rc=$?; printf '%s' \"$rc\" > {shlex.quote('/run/pocketverse/' + status_path.name)}"
    )
    created = subprocess.run(
        ["tmux", "-S", sock, "new-window", "-d", "-t", "pocket:",
         "sh", "-c", script, "pocket-exec", *command],
        capture_output=True, text=True, check=False,
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr.strip() or "tmux could not create exec window")
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        if status_path.exists():
            sys.stdout.write(out_path.read_text(errors="replace") if out_path.exists() else "")
            try:
                result = int(status_path.read_text().strip())
            except ValueError:
                result = 1
            for path in (out_path, status_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return result
        time.sleep(0.05)
    raise TimeoutError("timed out waiting for pocket exec")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pocketverse.sessionctl")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("serve", "attach", "detach"):
        p = sub.add_parser(action)
        p.add_argument("-c", "--config", required=True, type=Path)
        p.add_argument("--session", required=True)
        if action == "serve":
            p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("exec")
    p.add_argument("-c", "--config", required=True, type=Path)
    p.add_argument("--session", required=True)
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.action == "serve":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            return serve(args.config, args.session, command or None)
        cfg = load_config(args.config)
        if args.action == "attach":
            return attach(cfg, args.session)
        if args.action == "detach":
            return detach(cfg, args.session)
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return exec_command(cfg, args.session, command)
    except (FileNotFoundError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
