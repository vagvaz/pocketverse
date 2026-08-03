"""Command wrapper used in allowlist/port-forward modes, executed INSIDE bwrap.

Ownership: Lane 1 (@fixer). Implement to spec.

Usage::

    python3 -m pocketverse._inner [--port N --sock S] [--ports-json PATH] -- CMD [ARGS...]

Steps:
  1. If --port/--sock given (allowlist mode): spawn the proxy relay:
     subprocess.Popen([sys.executable, '-m', 'pocketverse.shim',
                       '--port', str(port), '--sock', sock]).
     Wait until 127.0.0.1:port accepts connections (timeout 5 s; if the
     shim process exits first, print its stderr and exit 4).
  2. If --ports-json given: read the port-forward list and start one
     unishim process per forward:
     subprocess.Popen([sys.executable, '-m', 'pocketverse.unishim',
                       '--sock', '/run/pocketverse/fwd-<target>.sock',
                       '--target', str(target)]).
     Do NOT block on readiness — the host-side shim retries until the unix
     socket appears.
  3. os.execvp(CMD[0], CMD).

The shim/unishim children are reaped automatically when the namespace dies
after the main command exits; no special signal handling needed here.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def _load_ports(path: str | None) -> list[dict]:
    """Read the port-forward list from a ports.json (or entry.json) file.

    Accepts either ``{"ports": [...]}`` or a bare JSON list.  Returns []
    for missing/unparseable files or empty lists.
    """
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    ports = data.get("ports") if isinstance(data, dict) else data
    if not isinstance(ports, list):
        return []
    return [p for p in ports if isinstance(p, dict) and p.get("target")]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pocketverse._inner",
        description="Start allowlist/port-forward relays and exec the user command.",
    )
    parser.add_argument("--port", type=int, default=None,
                        help="Allowlist shim TCP port (allowlist mode)")
    parser.add_argument("--sock", default=None,
                        help="Allowlist shim unix socket path (allowlist mode)")
    parser.add_argument("--ports-json", default=None,
                        help="Path to ports.json (sandbox-visible)")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    cmd: list[str] = args.cmd
    if not cmd:
        print(
            "usage: python3 -m pocketverse._inner [--port PORT --sock SOCK] "
            "[--ports-json PATH] -- CMD [ARGS...]",
            file=sys.stderr,
        )
        return 1

    # argparse.REMAINDER captures everything after the known options, including
    # a leading '--' separator.  Strip it if present.
    if cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("no command specified after '--'", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # 1. Allowlist relay shim (only when --port/--sock were given)
    # ------------------------------------------------------------------
    shim_proc = None
    if args.port is not None and args.sock:
        shim_proc = subprocess.Popen(
            [sys.executable, "-m", "pocketverse.shim",
             "--port", str(args.port),
             "--sock", args.sock],
            stderr=subprocess.PIPE,
        )

        deadline = time.monotonic() + 5.0
        connected = False
        last_error = ""

        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", args.port), timeout=0.5
                ):
                    connected = True
                    break
            except (ConnectionRefusedError, OSError) as exc:
                last_error = str(exc)
                # Did the shim die already?
                retcode = shim_proc.poll()
                if retcode is not None:
                    _, stderr_data = shim_proc.communicate()
                    print(f"shim exited with code {retcode}", file=sys.stderr)
                    if stderr_data:
                        print(stderr_data.decode(), file=sys.stderr)
                    return 4
                time.sleep(0.1)
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.1)

        if not connected:
            print(
                f"timed out waiting for shim on 127.0.0.1:{args.port}: "
                f"{last_error}",
                file=sys.stderr,
            )
            shim_proc.terminate()
            try:
                shim_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                shim_proc.kill()
            return 4

    # ------------------------------------------------------------------
    # 2. Port-forward unishim processes (do not block on readiness — the
    #    host-side shim retries until each unix socket appears)
    # ------------------------------------------------------------------
    ports = _load_ports(args.ports_json)
    for p in ports:
        target = int(p["target"])
        subprocess.Popen(
            [sys.executable, "-m", "pocketverse.unishim",
             "--sock", f"/run/pocketverse/fwd-{target}.sock",
             "--target", str(target)],
            stderr=subprocess.PIPE,
        )

    # ------------------------------------------------------------------
    # 3. Exec the user command
    # ------------------------------------------------------------------
    os.execvp(cmd[0], cmd)

    # Should never reach here
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
