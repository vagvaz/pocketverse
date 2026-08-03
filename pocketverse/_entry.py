"""Entry point executed INSIDE the outer unshare (userns+mountns, mapped-root).

Ownership: Lane 1 (@fixer). Implement to spec per launcher.py docstring.

Usage: python3 -m pocketverse._entry /path/to/entry.json

Steps:
  1. Parse entry.json (schema in launcher.py module docstring).
  2. For each overlay in the session: mount overlayfs at the staging dir:
         mount -t overlay overlay \
           -o lowerdir=<source>,upperdir=<upper>,workdir=<work>,userxattr \
           <session.root>/mnt/<i>
     Invoke via subprocess ['mount', '-t', 'overlay', 'overlay', '-o', opts,
     mnt]. The `userxattr` option is REQUIRED for unprivileged mounts
     (kernel >= 5.11). Do NOT enable metacopy. If mount fails, print stderr
     and exit 3.
  3. Assemble the bwrap argv exactly per launcher.py's 'bwrap argv
     requirements' and os.execvp('bwrap', argv).

Note: this module runs in a private mount namespace — the overlay mounts
here are invisible on the host and vanish when the namespace dies. State
(uppers) persists in the session dir; that is the whole point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Pure helper – assembled bwrap argv  (testable without namespaces)
# ---------------------------------------------------------------------------

def _overlay_opts(ov: dict) -> str:
    """Mount -o string for one overlay dict.

    lowerdir may chain continuation layers: ov['extra_lowers'] are HIGHER
    layers (e.g. a parent session's upper) stacked over ov['source'].
    """
    lowers = [str(x) for x in ov.get("extra_lowers", [])] + [ov["source"]]
    lower = ":".join(lowers)
    return (f"lowerdir={lower},upperdir={ov['upper']},"
            f"workdir={ov['work']},userxattr")


def build_bwrap_argv(entry: dict, overlays: list[dict], pkg_parent: str) -> list[str]:
    """Build bubblewrap argv from the parsed entry dict and overlay states.

    Parameters
    ----------
    entry : dict
        Parsed contents of entry.json (see launcher.py ENTRY CONTEXT).
    overlays : list[dict]
        Overlay state dicts, each with at least ``index``, ``mnt``, ``target``.
    pkg_parent : str
        Absolute path to the parent of the ``pocketverse`` package
        (ro-bound into the sandbox so ``python3 -m pocketverse._inner`` works).

    Returns
    -------
    list[str]
        argv suitable for ``os.execvp(argv[0], argv)``.
    """
    argv: list[str] = [entry.get("bwrap_path") or "bwrap"]

    cfg: dict = entry["config"]
    iso: dict = cfg.get("isolation", {})
    net: dict = cfg.get("network", {})
    net_mode: str = net.get("mode", "none")
    env_map: dict[str, str] = dict(entry.get("env", {}))
    cmd: list[str] = list(entry.get("command", ["bash"]))

    # -- isolation flags ---------------------------------------------------
    if iso.get("die_with_parent", True):
        argv.append("--die-with-parent")
    if iso.get("new_session", True):
        argv.append("--new-session")
    if iso.get("unshare_pid", True):
        argv.append("--unshare-pid")
    if iso.get("unshare_ipc", True):
        argv.append("--unshare-ipc")
    if iso.get("unshare_uts", True):
        argv.append("--unshare-uts")
    argv.extend(["--hostname", iso.get("hostname", "pocket")])

    if iso.get("clearenv", True):
        argv.append("--clearenv")

    # -- network -----------------------------------------------------------
    if net_mode in ("allowlist", "none"):
        argv.append("--unshare-net")

    # -- basic filesystems -------------------------------------------------
    argv.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])

    # -- system dirs  (skip silently if absent) ----------------------------
    if iso.get("system_dirs", True):
        for _d in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
            if os.path.exists(_d):
                argv.extend(["--ro-bind", _d, _d])

    # -- nix store (NixOS userland lives here; skip silently elsewhere) -----
    nix_store: bool = iso.get("bind_nix_store", True) and os.path.isdir("/nix/store")
    if nix_store:
        argv.extend(["--ro-bind", "/nix/store", "/nix/store"])
        # profile bin symlink farms into the store -> usable PATH inside
        for _d in ("/run/current-system/sw/bin",
                   os.path.expanduser("~/.nix-profile/bin")):
            if os.path.isdir(_d):
                argv.extend(["--ro-bind", _d, _d])

    # -- /etc files  (skip silently if absent) -----------------------------
    # /etc/static is NixOS's symlink-farm root for /etc (e.g. ssl/certs/*
    # point into it); /etc/pki is the Fedora-family CA location. Both are
    # needed so TLS CA bundles resolve inside the sandbox.
    if iso.get("etc_files", True):
        for _p in ("/etc/resolv.conf", "/etc/hosts", "/etc/ssl",
                    "/etc/ca-certificates", "/etc/static", "/etc/pki"):
            if os.path.exists(_p):
                argv.extend(["--ro-bind", _p, _p])

    # -- user-defined mounts -----------------------------------------------
    mounts: list[dict] = cfg.get("mounts", [])
    for i, m in enumerate(mounts):
        src: str = m["path"]
        target: str = m.get("target") or src
        mode: str = m.get("mode", "ro")

        if mode == "ro":
            argv.extend(["--ro-bind", src, target])
        elif mode == "rw":
            argv.extend(["--bind", src, target])
        elif mode == "overlay":
            # staging mount-point for this overlay (pre-created by state.new_session)
            staging: str | None = None
            for ov in overlays:
                if ov["index"] == i:
                    staging = ov["mnt"]
                    break
            if staging:
                argv.extend(["--bind", staging, target])

    # -- shared directory (live rw bind shared across sandboxes) ------------
    shared: dict | None = cfg.get("shared")
    if shared is not None:
        shared_path: str = shared["path"]
        shared_target: str = shared.get("target") or "/shared"
        argv.extend(["--bind", shared_path, shared_target])

    # -- proxy socket / port-forward socket bind ----------------------------
    # In allowlist mode the proxy socket lives in the sock dir; when port
    # forwards are configured the in-sandbox unishim sockets (and ports.json)
    # live there too.  Both ride the same bind-mounted dir at /run/pocketverse.
    ports: list[dict] = entry.get("ports") or []
    if net_mode == "allowlist" or ports:
        sock_dir_host: str | None = entry.get("sock_dir_host")
        if sock_dir_host:
            argv.extend(["--bind", sock_dir_host, "/run/pocketverse"])

    # -- pocketverse package (ro-bound at real path) -----------------------
    argv.extend(["--ro-bind", pkg_parent, pkg_parent])

    # -- environment variables ---------------------------------------------
    workdir: str | None = cfg.get("workdir")

    final_env: dict[str, str] = {}

    # Defaults  (only if not already in env_map from user config)
    default_path: str = "/usr/local/bin:/usr/bin:/bin"
    if nix_store:
        default_path = "/run/current-system/sw/bin:" + default_path
    defaults: dict[str, str] = {
        "PATH": default_path,
        "HOME": entry.get("agent_home") or workdir or "/",
        "USER": "pocket",
        "TERM": "xterm-256color",
    }
    for k, v in defaults.items():
        if k not in env_map:
            final_env[k] = v

    # Proxy vars  (allowlist only; do not override explicit user values)
    if net_mode == "allowlist":
        port: int = net.get("port", 3128)
        proxy_url: str = f"http://127.0.0.1:{port}"
        proxy_map: dict[str, str] = {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "ALL_PROXY": proxy_url,
            "NO_PROXY": "localhost,127.0.0.1",
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "all_proxy": proxy_url,
            "no_proxy": "localhost,127.0.0.1",
        }
        for k, v in proxy_map.items():
            if k not in env_map:
                final_env[k] = v

    # Port-forward env vars (POCKET_PORT_<NAME> = host port; user wins)
    for p in ports:
        name = p.get("name")
        if name:
            var = f"POCKET_PORT_{name.upper()}"
            if var not in env_map:
                final_env[var] = str(p["host"])

    # LLM endpoint override (mechanism only; user env wins)
    llm_base: str | None = cfg.get("llm", {}).get("base_url")
    if llm_base:
        for var in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
            if var not in env_map:
                final_env[var] = llm_base

    # User-supplied env (always wins)
    for k, v in env_map.items():
        final_env[k] = v

    # PYTHONPATH so python3 can find pocketverse inside the sandbox
    if "PYTHONPATH" not in final_env:
        final_env["PYTHONPATH"] = pkg_parent
    else:
        final_env["PYTHONPATH"] = f"{pkg_parent}:{final_env['PYTHONPATH']}"

    for k, v in final_env.items():
        argv.extend(["--setenv", k, v])

    # -- working directory -------------------------------------------------
    if workdir:
        argv.extend(["--chdir", workdir])

    # -- command -----------------------------------------------------------
    # _inner wraps the command when there is an allowlist shim to start OR
    # port-forward unishim processes to start (in any network mode).
    if net_mode == "allowlist" or ports:
        inner_cmd: list[str] = ["python3", "-m", "pocketverse._inner"]
        if net_mode == "allowlist":
            port = net.get("port", 3128)
            proxy_sock: str = entry.get(
                "proxy_sock_in_sandbox", "/run/pocketverse/proxy.sock"
            )
            inner_cmd.extend(["--port", str(port), "--sock", proxy_sock])
        if ports:
            inner_cmd.extend(["--ports-json", "/run/pocketverse/ports.json"])
        argv.extend([*inner_cmd, "--", *cmd])
    else:
        argv.extend(cmd)

    return argv


# ---------------------------------------------------------------------------
# entry-point main
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python3 -m pocketverse._entry <entry.json> [-- CMD...]",
              file=sys.stderr)
        return 1

    entry_path = Path(sys.argv[1])
    if not entry_path.exists():
        print(f"entry.json not found: {entry_path}", file=sys.stderr)
        return 1

    entry: dict = json.loads(entry_path.read_text())

    # Command override from argv after '--'
    try:
        dash_idx = sys.argv.index("--")
        if dash_idx + 1 < len(sys.argv):
            entry["command"] = sys.argv[dash_idx + 1:]
    except ValueError:
        pass

    cfg: dict = entry["config"]
    pkg_parent: str = entry["pkg_parent"]
    overlays: list[dict] = entry.get("overlays", [])

    # -- Mount overlay filesystems -----------------------------------------
    for ov in overlays:
        mnt: str = ov["mnt"]
        result = subprocess.run(
            ["mount", "-t", "overlay", "overlay", "-o", _overlay_opts(ov), mnt],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"overlay mount failed for {mnt}: {result.stderr.strip()}",
                  file=sys.stderr)
            return 3

    # -- Build and exec bwrap ----------------------------------------------
    bwrap_argv = build_bwrap_argv(entry, overlays, pkg_parent)
    os.execvp(bwrap_argv[0], bwrap_argv)

    # Should never reach here
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
