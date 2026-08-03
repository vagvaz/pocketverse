"""Host-side sandbox launcher.

Ownership: Lane 1 (@fixer). Implement to spec. Do not edit models.py or
state.py; import from them. You also own _entry.py and _inner.py.

Execution pipeline for ``pocket run``::

    pocketverse.launcher.run_sandbox()            (host, normal user)
      -> creates Session via state.new_session()
      -> if allowlist: spawn proxy subprocess (see below)
      -> unshare --user --map-root-user --mount -- <py> -m pocketverse._entry <meta> -- <cmd...>
           (outer userns+mountns: grants CAP_SYS_ADMIN so overlay mounts work)
        -> pocketverse._entry                     (inside outer ns, mapped-root)
             mount -t overlay ... per overlay mount (with userxattr!)
             os.execvp('bwrap', <bwrap argv>)     (the actual sandbox)
          -> [allowlist only] <py> -m pocketverse._inner --port N --sock S -- <cmd...>
               starts shim subprocess, then execs the user command

Package availability inside the sandbox: pocketverse may be installed in a
venv/user-site that does not exist inside the sandbox. The launcher must
therefore compute PKG_PARENT = Path(pocketverse.__file__).resolve().parent.parent
and pass it through meta-adjacent state (see ENTRY CONTEXT below) so _entry can
ro-bind it at the same absolute path and set PYTHONPATH to it. The python
interpreter used for _entry/_inner/shim is ``python3`` resolved via PATH inside
the sandbox (system_dirs ro-binds provide /usr/bin). Do **not** pass
sys.executable into the sandbox (venv paths break).

ENTRY CONTEXT: _entry needs more than meta.json provides. The launcher writes
an extra JSON file ``<session.root>/entry.json`` before exec'ing unshare::

    {
      "config": <cfg.model_dump(mode="json")>,        # resolved paths as strings
      "session_id": "...",
      "pkg_parent": "/abs/path",
      "env": { ... fully expanded env incl. proxy vars ... },
      "proxy_sock_in_sandbox": "/run/pocketverse/proxy.sock",   # allowlist only
      "command": ["..."]
    }

_entry reads ONLY this file (path is argv[1]); argv after '--' is the command
override (replacing config.command when non-empty).

Proxy lifecycle (allowlist mode):

  - Start BEFORE unshare::

        subprocess.Popen([sys.executable, '-m',
          'pocketverse.proxy', '--sock', str(session.proxy_sock),
          '--allow', RULE..., '--allow-port', PORT...,
          '--log', str(session.log_dir/'proxy.log')])

  - Poll until session.proxy_sock exists (timeout 5 s) before launching
    unshare, so the bind-mount source exists.
  - On sandbox exit (or launcher exception), terminate the proxy (SIGTERM,
    3 s grace, then SIGKILL).

bwrap argv requirements (assembled in _entry):

  - ``--die-with-parent`` / ``--new-session`` / ``--unshare-pid`` /
    ``--unshare-ipc`` / ``--unshare-uts`` / ``--hostname`` per cfg.isolation
  - ``--clearenv`` when isolation.clearenv (bwrap does NOT scrub env by default)
  - network: FULL -> nothing; ALLOWLIST|NONE -> ``--unshare-net``
  - ``--proc /proc``, ``--dev /dev``, ``--tmpfs /tmp``
  - isolation.system_dirs: ``--ro-bind`` each existing of
    ``/usr /bin /sbin /lib /lib64`` (skip silently when absent;
    /lib64 etc. may be symlinks -> --ro-bind follows fine, but guard with
    os.path.exists)
  - isolation.etc_files: ``--ro-bind`` existing of
    ``/etc/resolv.conf /etc/hosts /etc/ssl /etc/ca-certificates``
  - mounts: ro -> ``--ro-bind src target``; rw -> ``--bind src target``;
    overlay -> ``--bind session_mnt_i target`` (staging mount, see _entry)
  - allowlist: ``--bind session.sock_dir /run/pocketverse`` (gives the sandbox
    access ONLY to the proxy socket; /run is created via --tmpfs? No: bwrap
    creates destination parents automatically)
  - ``--setenv`` for every entry in entry.json env, plus PATH/HOME/USER/TERM
    defaults (PATH=/usr/local/bin:/usr/bin:/bin, HOME=<workdir or />,
    USER=pocket, TERM=xterm-256color) unless already present in env
  - allowlist adds proxy vars: HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (and
    lowercase) = http://127.0.0.1:<port>, NO_PROXY/no_proxy=localhost,127.0.0.1
  - ``--chdir workdir`` when set (bwrap errors if missing -> validate earlier)
  - final command: allowlist -> [python3, -m, pocketverse._inner, '--port',
    str(port), '--sock', proxy_sock_in_sandbox, '--', *cmd]; else -> cmd
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pocketverse
from . import agents
from . import telemetry
from .models import AgentType, Mount, MountMode, SandboxConfig
from .state import Session, SESSION_PREFIX, record_overlay_updates, record_worktrees


def _load_continuation(cfg: SandboxConfig, continue_from: str) -> dict[int, dict]:
    """Load the parent session and build per-overlay-index continuation hints.

    Raises FileNotFoundError (via state.load_session) for unknown sessions.
    """
    from . import state
    parent = state.load_session(cfg, continue_from)
    parent_meta = json.loads(parent.meta_path.read_text())
    hints: dict[int, dict] = {}
    for ov_meta in parent_meta.get("overlays", []):
        hints[ov_meta["index"]] = {
            "upper": ov_meta["upper"],
            "chain_depth": ov_meta.get("chain_depth", 1),
            "worktree": ov_meta.get("worktree"),
        }
    return hints


def _resolve_overlay_continuation(
    cfg: SandboxConfig,
    sess: Session,
    hints: dict[int, dict],
) -> dict[int, list[str]]:
    """Chain the parent's state into each non-worktree overlay's lower.

    chain_depth < 2: parent's upper becomes an extra lower layer
    (returned as extra_lowers for entry.json), depth +1 recorded.
    chain_depth >= 2: COMPACT — merge base + parent upper into a fresh
    directory and use it as the single lower (whiteout semantics across
    3+ lowerdir components are not load-bearing). Returns the
    extra_lowers map for entry.json serialization.
    """
    from . import state
    extra: dict[int, list[str]] = {}
    updates: dict[int, dict] = {}
    for ov in sess.overlays:
        m = cfg.mounts[ov.index]
        if m.worktree is not None:
            continue  # worktree continuation handled by _setup_worktrees
        hint = hints.get(ov.index)
        if not hint:
            continue
        if hint["chain_depth"] >= 2:
            compact_dir = (cfg.state_dir / ".compacted" / cfg.name /
                           f"{sess.id}-{ov.index}")
            compact_dir.parent.mkdir(parents=True, exist_ok=True)
            if compact_dir.exists():
                shutil.rmtree(compact_dir)
            shutil.copytree(ov.source, compact_dir, symlinks=True)
            fake = Session(
                id=f"compact-{sess.id}", root=sess.root,
                config_name=sess.config_name,
                overlays=[type(ov)(index=ov.index, source=compact_dir,
                                   target=ov.target, upper=Path(hint["upper"]),
                                   work=ov.work, mnt=ov.mnt)],
            )
            result = state.apply_session(fake, backup=False)
            if result.errors:
                raise RuntimeError(
                    f"continuation compaction failed for mount {ov.index}: "
                    + "; ".join(result.errors))
            updates[ov.index] = {"source": str(compact_dir), "chain_depth": 1}
        else:
            extra[ov.index] = [str(hint["upper"])]
            updates[ov.index] = {"chain_depth": hint["chain_depth"] + 1}
    if updates:
        record_overlay_updates(sess, updates)
    return extra


def _setup_worktrees(cfg: SandboxConfig, sess: Session,
                     hints: dict[int, dict] | None = None) -> None:
    """Create per-session git worktrees for overlay mounts with `worktree:`.

    Redirects each such overlay's lower to the fresh worktree (persisted
    into meta.json via record_worktrees so diff/apply target the worktree)
    and appends rw binds for the repo's git dir(s) — commits inside the
    sandbox land in the real repo's object store. Branch naming: 'auto' ->
    session-<session-id>; explicit branch names are created with -b on
    first use, reused after (note: git refuses checking out one branch in
    two worktrees simultaneously).
    """
    updates: dict[int, dict] = {}
    for ov in sess.overlays:
        m = cfg.mounts[ov.index]
        if m.worktree is None:
            continue
        repo = Path(m.worktree.repo) if m.worktree.repo else m.path

        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-dir"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"worktree: {repo} is not a git repository: {r.stderr.strip()}")
        gitdir = r.stdout.strip()
        if not os.path.isabs(gitdir):
            gitdir = str((repo / gitdir).resolve())
        rc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True)
        commondir = rc.stdout.strip() if rc.returncode == 0 else gitdir
        if not os.path.isabs(commondir):
            commondir = str((repo / commondir).resolve())

        branch = f"session-{sess.id}" if m.worktree.branch == "auto" else m.worktree.branch
        wt_root = cfg.state_dir / ".worktrees" / cfg.name
        wt_root.mkdir(parents=True, exist_ok=True)
        wt_path = wt_root / f"{sess.id}-{ov.index}"

        # continuation: start the new branch at the parent's branch tip
        start_point: str | None = None
        hint_wt = (hints or {}).get(ov.index, {}).get("worktree")
        if hint_wt and hint_wt.get("repo") == str(repo) and hint_wt.get("branch"):
            start_point = hint_wt["branch"]

        args = ["git", "-C", str(repo), "worktree", "add", str(wt_path)]
        exists = subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
             f"refs/heads/{branch}"])
        if exists.returncode == 0:
            args += [branch]
        else:
            args += ["-b", branch, start_point or "HEAD"]
        r2 = subprocess.run(args, capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {r2.stderr.strip()}")

        updates[ov.index] = {
            "source": str(wt_path),
            "worktree": {"repo": str(repo), "branch": branch,
                         "path": str(wt_path),
                         "gitdir": gitdir, "commondir": commondir},
        }
        for gd in dict.fromkeys([gitdir, commondir]):
            if not any(mm.path == Path(gd) for mm in cfg.mounts):
                cfg.mounts.append(Mount(path=Path(gd), target=Path(gd),
                                        mode=MountMode.RW))
    if updates:
        record_worktrees(sess, updates)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_sandbox(
    cfg: SandboxConfig,
    *,
    session_id: str | None = None,
    cmd: list[str] | None = None,
    dry_run: bool = False,
    continue_from: str | None = None,
    tmux_session: bool = False,
) -> int:
    """Run the sandbox; return the sandboxed command's exit code.

    Parameters
    ----------
    cfg : SandboxConfig
        Loaded configuration.
    session_id : str, optional
        Explicit session id (single-use).  *Not* "re-attach" — always creates
        a fresh session.
    cmd : list[str], optional
        Override the config's ``command``.
    dry_run : bool
        If True, print the would-be ``unshare`` command line (shlex-quoted) to
        stdout and return 0 without creating anything.

    Returns
    -------
    int
        Exit code of the sandboxed command.
    """
    # -- validate workdir early --------------------------------------------
    if cfg.workdir:
        _validate_workdir(cfg)

    # -- dry-run: just print & return (no bwrap/prlimit required) -----------
    if dry_run:
        fake_session_dir = cfg.state_dir / cfg.name / f"{SESSION_PREFIX}dummy"
        entry_json = fake_session_dir / "entry.json"
        unshare_cmd = build_unshare_command(entry_json)
        prefix = _prlimit_prefix(cfg, require=False)
        print(shlex.join([*prefix, *unshare_cmd]))
        return 0

    # -- resolve bwrap on the host; fail fast with a readable error ---------
    bwrap_path = shutil.which("bwrap")
    if bwrap_path is None:
        raise FileNotFoundError(
            "bubblewrap ('bwrap') not found on PATH — install bubblewrap "
            "(e.g. 'nix profile install nixpkgs#bubblewrap' or your distro's "
            "package manager) and retry"
        )

    # -- shared directory: live rw channel, created on the host at launch ---
    if cfg.shared is not None:
        cfg.shared.path.mkdir(parents=True, exist_ok=True)

    # -- copy-on-write for programmatic mount additions ----------------------
    if cfg.agent.type is not AgentType.SHELL or any(m.worktree for m in cfg.mounts):
        cfg = cfg.model_copy(deep=True)

    # -- agent runtime: persistent state mounts + stable HOME + command -----
    if cfg.agent.type is not AgentType.SHELL:
        for m in agents.state_mounts(cfg):
            m.path.mkdir(parents=True, exist_ok=True)  # rw bind source must exist
        cfg.mounts.extend(agents.state_mounts(cfg))
        if cmd is None:  # human pocket: interactive agent CLI
            cmd = agents.build_command(
                cfg, None,
                session_id=str(uuid.uuid4()) if cfg.agent.type is AgentType.CLAUDE else None,
                resume_native_id=None,
                interactive=True,
            )

    # -- normal run --------------------------------------------------------

    # 1. Create session dirs
    from . import state  # lane 3 owns this; assume it works
    sess: Session = state.new_session(cfg, session_id)
    telemetry_session = telemetry.start_session(sess.id, sess.root, cfg.telemetry)
    telemetry_session.emit_event("session_prepared", attributes={
        "config": cfg.name,
        "network_mode": cfg.network.mode.value,
        "agent_type": cfg.agent.type.value,
    })

    # 1b. Continuation hints from the parent session (if any), then
    #     per-session git worktrees + overlay lower chaining/compaction.
    hints: dict[int, dict] = {}
    if continue_from:
        hints = _load_continuation(cfg, continue_from)
    _setup_worktrees(cfg, sess, hints or None)
    extra_lowers = (_resolve_overlay_continuation(cfg, sess, hints)
                    if continue_from else {})

    # 2. Compute package parent for PYTHONPATH
    pkg_parent: str = str(
        Path(pocketverse.__file__).resolve().parent.parent
    )

    # 3. Build expanded env map (user-defined only; defaults/proxy added by
    #    build_bwrap_argv in _entry.py)
    expanded_env: dict[str, str] = cfg.expanded_env()

    # 4. Build entry dict
    entry: dict[str, object] = {
        "config": cfg.model_dump(mode="json"),
        "session_id": sess.id,
         "bwrap_path": bwrap_path,
         "pkg_parent": pkg_parent,
         "env": expanded_env,
         "command": [str(c) for c in (cmd if cmd is not None else cfg.command)],
         "overlays": [],
     }

    if tmux_session:
        base_command = list(cmd if cmd is not None else cfg.command)
        # Start tmux detached (no controlling terminal exists in the server
        # process), then keep this foreground wrapper alive until the tmux
        # session exits so bwrap's parent/death lifecycle remains intact.
        cmd = ["sh", "-c",
               "tmux -S /run/pocketverse/tmux.sock new-session -d -s pocket -- \"$@\"; "
               "while tmux -S /run/pocketverse/tmux.sock has-session -t pocket 2>/dev/null; "
               "do sleep 1; done",
               "pocket-tmux", *base_command]
        entry["command"] = cmd

    if cfg.agent.type is not AgentType.SHELL:
        entry["agent_home"] = agents.AGENT_HOME

    # Serialise overlay state for _entry
    overlays_data: list[dict] = []
    for ov in sess.overlays:
        overlays_data.append({
            "index": ov.index,
            "source": str(ov.source),
            "target": str(ov.target),
            "upper": str(ov.upper),
            "work": str(ov.work),
            "mnt": str(ov.mnt),
            **({"extra_lowers": extra_lowers[ov.index]}
               if ov.index in extra_lowers else {}),
        })
    entry["overlays"] = overlays_data

    # 5. Proxy (allowlist only) + port-forward host shims.
    #    Every child spawned here is terminated in the finally block.
    child_procs: list[subprocess.Popen] = []
    proxy_proc: subprocess.Popen | None = None

    if cfg.network.mode == "allowlist" or cfg.ports or tmux_session:
        # The sock dir is bind-mounted at /run/pocketverse inside the sandbox
        # so the in-sandbox shim/unishim processes can reach the host relays.
        entry["sock_dir_host"] = str(sess.sock_dir)

    if cfg.network.mode == "allowlist":
        entry["proxy_sock_in_sandbox"] = "/run/pocketverse/proxy.sock"

        proxy_args: list[str] = [
            sys.executable, "-m", "pocketverse.proxy",
            "--sock", str(sess.proxy_sock),
        ]
        for rule in cfg.network.allow:
            proxy_args.extend(["--allow", rule])
        for port in cfg.network.allow_ports:
            proxy_args.extend(["--allow-port", str(port)])
        proxy_args.extend(["--log", str(sess.log_dir / "proxy.log")])

        proxy_proc = subprocess.Popen(proxy_args)
        child_procs.append(proxy_proc)

        # Poll until socket appears
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if sess.proxy_sock.exists():
                break
            if proxy_proc.poll() is not None:
                _, stderr = proxy_proc.communicate()
                msg = stderr.decode() if stderr else "(no stderr)"
                print(f"proxy exited prematurely:\n{msg}", file=sys.stderr)
                return 1
            time.sleep(0.1)
        else:
            print("timed out waiting for proxy socket", file=sys.stderr)
            _terminate_procs(child_procs)
            return 1

    # -- port forwards ------------------------------------------------------
    if cfg.ports:
        ports: list[dict] = _allocate_ports(cfg)
        entry["ports"] = ports

        # ports.json: canonical copy for the CLI, plus a copy inside the sock
        # dir so the in-sandbox _inner (which sees sock_dir at /run/pocketverse)
        # can read the forwards.
        ports_doc: dict[str, object] = {"session_id": sess.id, "ports": ports}
        sess.root.joinpath("ports.json").write_text(json.dumps(ports_doc, indent=2))
        sess.sock_dir.joinpath("ports.json").write_text(json.dumps(ports_doc, indent=2))

        for p in ports:
            fwd_sock: Path = sess.sock_dir / f"fwd-{p['target']}.sock"
            shim_args: list[str] = [
                sys.executable, "-m", "pocketverse.shim",
                "--port", str(p["host"]),
                "--sock", str(fwd_sock),
                "--bind", "127.0.0.1",
            ]
            child_procs.append(subprocess.Popen(shim_args))

            line: str = f"port: 127.0.0.1:{p['host']} -> sandbox:{p['target']}"
            if p["name"]:
                line += f" ({p['name']})"
            print(line, file=sys.stderr)

    # 6. Write entry.json
    entry_json: Path = sess.root / "entry.json"
    entry_json.parent.mkdir(parents=True, exist_ok=True)
    entry_json.write_text(json.dumps(entry, indent=2))

    if tmux_session:
        (sess.root / "run.json").write_text(json.dumps({
            "session_id": sess.id, "pid": os.getpid(), "started": time.time(),
        }))

    # 7. Launch unshare (optionally wrapped in prlimit for resource limits)
    unshare_cmd: list[str] = build_unshare_command(entry_json)
    launch_cmd: list[str] = [*_prlimit_prefix(cfg), *unshare_cmd]

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(launch_cmd)
        telemetry_session.emit_event(
            "sandbox_started", attributes={"pid": getattr(proc, "pid", None)})
        returncode = proc.wait()
        telemetry_session.emit_event("sandbox_finished", attributes={"exit_code": returncode})
        return returncode
    except KeyboardInterrupt:
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            return proc.wait()
        raise
    finally:
        if child_procs:
            _terminate_procs(child_procs)
        if tmux_session:
            try:
                (sess.root / "run.json").unlink()
            except FileNotFoundError:
                pass
        telemetry.end_session(telemetry_session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_workdir(cfg: SandboxConfig) -> None:
    """Raise ``ValueError`` if ``cfg.workdir`` is not under a mount target
    or ``/tmp``."""
    if not cfg.workdir:
        return
    # Collect all mount targets and the always-available /tmp
    allowed: list[str] = [str(Path("/tmp"))]
    for m in cfg.mounts:
        allowed.append(str(m.resolved_target))
    # Longer prefixes first  (e.g. /tmp/foo before /tmp)
    allowed.sort(key=len, reverse=True)
    if not any(cfg.workdir.startswith(p) for p in allowed):
        raise ValueError(
            f"workdir {cfg.workdir!r} is not under any mount target or /tmp. "
            f"Allowed prefixes: {allowed}"
        )


def _prlimit_prefix(cfg: SandboxConfig, *, require: bool = True) -> list[str]:
    """Return a prlimit(1) prefix wrapping the sandbox when limits are set.

    Returns ``[]`` when no user limit fields are configured (cpu, memory,
    file_size, open_files and processes are all None).  Otherwise returns
    ``['prlimit', *cfg.limits.prlimit_args()]``.

    With ``require=True`` (default), the prlimit binary is resolved via
    ``shutil.which`` and a ``FileNotFoundError`` with a readable message is
    raised when it is absent.  Pass ``require=False`` for dry-run output,
    which must display the command without requiring prlimit to be installed.
    """
    lim = cfg.limits
    if (lim.cpu is None and lim.memory is None and lim.file_size is None
            and lim.open_files is None and lim.processes is None):
        return []
    if require:
        if shutil.which("prlimit") is None:
            raise FileNotFoundError(
                "prlimit (util-linux) not found on PATH — install util-linux "
                "to use resource limits"
            )
    return ["prlimit", *lim.prlimit_args()]


def _allocate_ports(cfg: SandboxConfig) -> list[dict]:
    """Allocate a concrete host port for every configured port forward.

    Returns ``[{'name', 'target', 'host'}]`` in config order.  Auto-allocated
    host ports are picked by binding ``127.0.0.1:0`` and immediately closing —
    note the inherent small race between that close and the host shim
    re-binding the same port (acceptable for a CLI tool).  Raises
    ``ValueError`` on duplicate targets.
    """
    seen: set[int] = set()
    allocated: list[dict] = []
    for pf in cfg.ports:
        if pf.target in seen:
            raise ValueError(f"duplicate port forward target: {pf.target}")
        seen.add(pf.target)

        if pf.host is not None:
            host_port = pf.host
        else:
            # Bind port 0 -> kernel picks a free ephemeral port, then release.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                host_port = s.getsockname()[1]

        allocated.append({
            "name": pf.name,
            "target": pf.target,
            "host": host_port,
        })
    return allocated


def _terminate_procs(procs: list[subprocess.Popen]) -> None:
    """SIGTERM all running procs, 3 s grace, then SIGKILL each straggler."""
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def build_unshare_command(entry_json: Path) -> list[str]:
    """Return the outer ``unshare`` command line.

    Shape::

        ['unshare', '--user', '--map-root-user', '--mount', '--',
         sys.executable, '-m', 'pocketverse._entry', str(entry_json)]
    """
    return [
        "unshare", "--user", "--map-root-user", "--mount", "--",
        sys.executable, "-m", "pocketverse._entry", str(entry_json),
    ]
