# pocketverse

Composable unprivileged sandboxes for AI agents — bubblewrap + overlayfs
copy-on-write dirs + domain-allowlist network proxy, all from one YAML file.

## Requirements

- Linux kernel ≥ 5.11 (`userxattr` overlayfs support for unprivileged mounts)
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap` in PATH;
  on NixOS: `nix profile install nixpkgs#bubblewrap`)
- `util-linux` (`unshare` with `--user --map-root-user`)
- Python ≥ 3.11
- **NixOS**: works out of the box — `/nix/store`, the profile bin symlink
  farms, and `/etc/static` (the symlink target of `/etc/ssl/certs/*`) are
  read-only bound automatically; see `isolation.bind_nix_store`.
- **Ubuntu 24.04+ caveat**: `kernel.apparmor_restrict_unprivileged_userns=1` by
  default blocks unprivileged user namespaces.  Workaround:

  ```console
  $ sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
  ```

## Install

```console
$ pip install -e .
```

Entry point: `pocket`.

## Quickstart

```console
# Generate an annotated config
$ pocket init

# Edit to match your paths and API keys
$ $EDITOR pocketverse.yaml

# Launch a sandbox with an interactive shell
$ pocket run -- bash

# Inside the sandbox, make changes under /workspace
# (or whatever your overlay mount target is)
pocket$ touch workspace/new-file.txt
pocket$ exit

# See what changed
$ pocket diff

# Apply the changes back to the host filesystem
$ pocket apply
```

## YAML reference

| Key                               | Type          | Default           | Description |
|-----------------------------------|---------------|-------------------|-------------|
| `version`                         | int           | `1`               | Schema version |
| `name`                            | str           | `"default"`       | Human-readable name; used as a subdirectory under `state_dir` |
| `mounts[].path`                   | str           | (required)        | Host directory path (`~` / `$VAR` expanded) |
| `mounts[].target`                 | str           | same as `path`    | Path inside the sandbox |
| `mounts[].mode`                   | str           | `"ro"`            | `"ro"` (read-only bind), `"rw"` (read-write bind), or `"overlay"` (copy-on-write via overlayfs) |
| `mounts[].worktree.repo`          | path          | mount `path`      | Git repo backing the overlay (only valid with `mode: overlay`) |
| `mounts[].worktree.branch`        | str           | `"auto"`          | Worktree branch; `auto` = `session-<session-id>`. Explicit names are created with `-b` on first use (one branch can't be checked out in two worktrees at once) |
| `shared.path`                     | str           | (required)        | Host directory shared **live** (rw) across concurrent sandboxes; created at launch if missing |
| `shared.target`                   | str           | `"/shared"`       | Path inside the sandbox where the shared directory is bound |
| `limits.cpu`                      | int           | `null`            | `RLIMIT_CPU` — total CPU seconds before `SIGXCPU` |
| `limits.memory`                   | str/int       | `null`            | `RLIMIT_AS` — address-space limit per process, e.g. `"4G"`, `"512M"` (base-1024 suffixes) |
| `limits.file_size`                | str/int       | `null`            | `RLIMIT_FSIZE` — max size of a created file, e.g. `"1G"` |
| `limits.open_files`               | int           | `null`            | `RLIMIT_NOFILE` — max open file descriptors |
| `limits.processes`                | int           | `null`            | `RLIMIT_NPROC` — **caveat:** counted against your real uid across the whole desktop, not just the sandbox |
| `limits.core`                     | int           | `0`               | `RLIMIT_CORE` — core-dump size in bytes (0 disables dumps) |
| `llm.base_url`                    | str           | `null`            | Inject `ANTHROPIC_BASE_URL` + `OPENAI_BASE_URL` into the sandbox (mechanism only — lets you route background agents through a different endpoint/lane; remote endpoints also need their domain in `network.allow`) |
| `agent.type`                      | str           | `"shell"`         | Agent runtime: `shell` (plain command), `claude-code`, or `opencode`. Non-shell pockets get a stable `HOME=/home/pocket`, persistent per-config agent-state mounts, and CLI command construction (see below) |
| `agent.max_budget_usd`            | float         | `null`            | claude-code: passed as `--max-budget-usd` (native spend cap) |
| `ports[].target`                  | int           | (required)        | Sandbox TCP port (on the sandbox loopback) to publish on the host |
| `ports[].host`                    | int           | `null` (auto)     | Host port on `127.0.0.1`; a free port is auto-allocated when `null` |
| `ports[].name`                    | str           | `null`            | If set, the host port is injected into the sandbox as `POCKET_PORT_<NAME>` (uppercased) |
| `network.mode`                    | str           | `"none"`          | `"full"` (host network), `"allowlist"` (domain proxy), `"none"` (loopback only) |
| `network.allow`                   | list\[str\]   | `[]`              | Domain allowlist rules — `"example.com"`, `"*.example.com"`, `"example.com:443"` |
| `network.port`                    | int           | `3128`            | Port the in-sandbox relay listens on (`127.0.0.1` inside) |
| `network.allow_ports`             | list\[int\]   | `[443, 80]`       | Upstream ports the proxy may connect to (unless a rule pins a port) |
| `env`                             | dict          | `{}`              | Environment variables; values undergo `$VAR` / `${VAR}` expansion from the host |
| `workdir`                         | str           | `null`            | Working directory inside the sandbox (must be under a mount target or `/tmp`) |
| `command`                         | list\[str\]   | `["bash"]`        | Default command executed inside the sandbox |
| `state_dir`                       | str           | `".pocket/state"` | Root for session state (overlay uppers, proxy socket, metadata) |
| `isolation.die_with_parent`       | bool          | `true`            | Kill the sandbox if the parent process dies |
| `isolation.new_session`           | bool          | `true`            | `setsid()` inside the sandbox (blocks TIOCSTI terminal injection) |
| `isolation.unshare_pid`           | bool          | `true`            | Private PID namespace |
| `isolation.unshare_ipc`           | bool          | `true`            | Private IPC namespace |
| `isolation.unshare_uts`           | bool          | `true`            | Private UTS (hostname) namespace |
| `isolation.hostname`              | str           | `"pocket"`        | Sandbox hostname |
| `isolation.clearenv`              | bool          | `true`            | `bwrap --clearenv` — start with an empty environment |
| `isolation.system_dirs`           | bool          | `true`            | Read-only bind `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64` |
| `isolation.bind_nix_store`        | bool          | `true`            | Read-only bind `/nix/store` + profile bin dirs, and prefix sandbox PATH with `/run/current-system/sw/bin` (no-op when `/nix/store` is absent) |
| `isolation.etc_files`             | bool          | `true`            | Read-only bind `/etc/resolv.conf`, `/etc/hosts`, `/etc/ssl`, `/etc/ca-certificates`, `/etc/static`, `/etc/pki` |

## How it works

1. **`pocket run`** loads the YAML config and creates a session directory tree
   under `state_dir/<name>/session-<id>/`.  For each overlay mount it creates
   `upper/<i>/`, `work/<i>/`, and `mnt/<i>/`.

2. In **allowlist** mode the host starts a domain-allowlist forward proxy
   (asyncio-based, zero third-party dependencies) listening on a UNIX socket
   inside the session `sock/` directory.

3. The launcher calls `unshare --user --map-root-user --mount --` to enter a
   user + mount namespace as mapped root, granting `CAP_SYS_ADMIN` for overlay
   mounts without host privileges.

4. Inside that outer namespace, `_entry.py` mounts each overlay filesystem
   (`-o userxattr,lowerdir=...,upperdir=...,workdir=...`) at the pre-created
   staging directory, then `os.execvp`'s `bwrap` to build the actual sandbox.

5. **bwrap** creates the inner namespaces (pid, net, ipc, uts), binds
   ro/rw/overlay mounts, and in allowlist mode bind-mounts the proxy socket
   into `/run/pocketverse/` inside the sandbox; the same `/run/pocketverse`
   bind carries the port-forward unix sockets and `ports.json` when forwards
   are configured.  Because UNIX sockets are not netns-namespaced, these are
   the only egress/ingress points when `--unshare-net` is active.

6. In **allowlist** mode `_inner.py` spawns a shim that exposes the UNIX socket
   as `127.0.0.1:<port>` via a local TCP relay, then `exec`s the user command.
   `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` (and lowercase variants)
   are set automatically in the environment.  Processes that respect these
   variables have internet access through the allowlist proxy; everything else
   (static binaries, programs that hardcode IP addresses) gets **no network**
   at all — this is the fail-closed design.

7. After the command exits, the outer namespace tears down and all overlay
   mounts vanish.  The session upper directories persist on disk.

8. **`pocket diff`** reads the session meta and walks the upper vs. lower
   directories using overlayfs semantics: whiteout markers (character device
   with rdev 0/0, or zero-size files with `user.overlay.whiteout` xattr),
   opaque directories (`user.overlay.opaque` xattr), and size/mtime comparison
   for regular files.

9. **`pocket apply`** copies changes back to the source directories with an
   optional timestamped backup under `session-<id>/backup-<ts>/`.

### `shared` — the live channel

**`shared`** is a plain read-write bind of a host directory, mounted identically
in every sandbox that declares it.  It is the live channel for inter-agent
exchange: two concurrent sandboxes (or the host) can hand files to each other
through it, and changes are reflected immediately on the host because it is a
direct bind, not an overlay.  Consequently `pocket diff` / `pocket apply` give
it **no coverage** — for gated, reviewable work use an overlay mount instead.

### `limits` — resource caps via prlimit(1)

**`limits`** wraps the whole sandbox pipeline in `prlimit(1)` (util-linux):
when any limit field is set, the outer `unshare` runs under prlimit, and
because RLIMITs are inherited across fork/exec, bwrap, the shim, and the agent
inside all inherit the caps.  Note that `limits.processes` (`RLIMIT_NPROC`) is
counted against your real uid across the entire desktop, not just the sandbox —
set it too low and process creation breaks everywhere.

### `ports` — publishing sandbox TCP on the host

**`ports`** lets agents run dev servers that are reachable from the host.  Each
forward is a relay pair over a bind-mounted UNIX socket — the same trick as the
allowlist proxy — so it works in **every** network mode even though the sandbox
netns is private: a host-side shim listens on `127.0.0.1:<host>` and forwards
into the socket, while an in-sandbox `unishim` relays from that socket to
`127.0.0.1:<target>`.  Publish nothing to non-loopback interfaces: the relays
are hard-bound to `127.0.0.1` only.

## Multi-agent operation

pocketverse includes a minimal, file-based coordination plane for running
multiple agents: **mailboxes** on the shared dir plus a host-side
**supervisor** that turns spawn requests into pockets.

```
shared/
  mailbox/<recipient>/in.jsonl   # one JSON message per line (atomic appends)
  mailbox/supervisor/.cursor     # supervisor's consumed-offset cursor
  requests/<msg-id>.json         # durable spool of consumed spawn requests
  sessions/<id>.json             # per-child manifest (status, pid, exit_code…)
  sessions/<id>.yaml             # the fully-resolved child config
  goals/<id>.md                  # the child's goal as a file
  logs/<id>.log                  # child stdout/stderr
```

- **Mailbox** — `append` is a single `O_APPEND` write (torn-write safe);
  readers use byte-offset cursors (at-least-once, non-destructive). Anyone
  can be a recipient (`human`, `supervisor`, a session id…). Message types
  are conventional: `spawn_request`, `progress`, `question`, `answer`,
  `done`, `failed`, `error`.
- **Supervisor** (`pocket supervisor -c cfg`) polls the `supervisor`
  mailbox for `spawn_request` payloads (`sender`, `goal`, `depends_on`,
  `continue_from`, `name`, `config`) and launches `pocket run` children.
  It enforces `supervisor.max_concurrent` and
  `supervisor.max_fanout_per_parent`, waits for `depends_on` sessions to
  reach `done`, reaps children into manifests, and mailbox-notifies the
  requester. Requests are spooled to `requests/` before the cursor
  advances, so a supervisor crash never silently loses one; on restart,
  orphaned running manifests (dead pid) are reaped as failed.
- **Child env**: launched pockets get `POCKET_GOAL`, `POCKET_GOAL_FILE`,
  `POCKET_SESSION_ID`, `POCKET_SENDER`, and `POCKET_CONTINUE_FROM` (when
  set). A spawn `config` of `null` inherits the supervisor's config; a
  mapping is validated as a full SandboxConfig; a string is a YAML path.

Inside a sandbox, agents use the same CLI via the bound-in package:
`python3 -m pocketverse.cli mailbox-send …` (needs `pydantic`+`pyyaml`
available to the in-sandbox python — mount a suitable environment or
install the package into the workspace).

### Agent runtimes (`agent:`)

With `agent.type: claude-code` or `opencode`, pockets host the real agent
CLI instead of a plain command:

- `pocket run` (human) launches the interactive CLI (`claude` / `opencode`);
  supervisor children launch headless (`claude -p <goal> --output-format json`
  / `opencode run <goal> --format json`).
- **Native session ids**: claude sessions get an orchestrator-chosen UUID
  (`--session-id`); opencode ids are captured from the JSON event log.
  Both land in the session manifest as `agent_session_id`.
- **Resume across pockets**: agent state lives in persistent per-config
  mounts (`<state_dir>/.agent-state/<name>/…` → `/home/pocket/.claude`,
  `…/opencode`), and spawn requests with `continue_from: <session-id>`
  resume the parent's native conversation (`claude --resume <id>` /
  `opencode run --session <id>`).
- claude-code only: `agent.max_budget_usd` maps to `--max-budget-usd`.

### Worktree mounts (`mounts[].worktree`)

An overlay mount with a `worktree:` block is backed by a per-session
**git worktree** instead of the original directory:

```yaml
mounts:
  - path: ~/project
    target: /workspace
    mode: overlay
    worktree: {}            # repo defaults to path; branch: auto -> session-<id>
```

On run, pocketverse executes `git worktree add` on a session branch and
points the overlay lower at the worktree; the repo's real git dir is
rw-bound into the sandbox at its absolute path so **agent commits land in
the real repo's object store**. Consequences:

- Review: `git log session-<id>` / `git diff main..session-<id>` on the host.
- Merge (the worktree-mode replacement for `pocket apply`): `git merge
  session-<id>`. `pocket diff`/`apply` still work and target the WORKTREE
  — use them for uncommitted leftovers at exit, then commit normally.
- The original checkout is never touched by the agent.
- Caveats: git's mtime-based stat cache can be confused by overlay
  copy-ups (a `git status` recheck fixes it); explicit branch names can't
  be checked out in two concurrent sessions; worktrees accumulate under
  `<state_dir>/.worktrees/<name>/` — prune with `git worktree prune`
  after merging.

### Continuation (`--continue-from`)

A pocket can continue another session's work — both the conversation
(via the agent runtime's native resume, wired in step 3) and the files:

```
pocket run -c cfg --continue-from <parent-session-id>
```

- **Worktree mounts**: the child's session branch starts at the parent's
  branch tip (not main HEAD), so the child sees the parent's commits.
- **Plain overlay mounts**: the parent's upper becomes an extra lower
  layer (depth cap 2). At depth 2, the next continuation **compacts** —
  merges base + parent upper into a fresh directory and uses it as the
  single lower (whiteout semantics across 3+ lowerdir components are not
  load-bearing).

The supervisor passes `--continue-from` automatically when a spawn
request has `continue_from: <session-id>`.

## Security posture

pocketverse provides **best-effort unprivileged isolation** using Linux kernel
primitives:

- **Read-only bind mounts** (`mode: ro`) are kernel-enforced; the sandbox
  process cannot write to these paths regardless of user id.
- **`--unshare-net`** in allowlist/none mode gives the sandbox a private
  network namespace with only loopback.  The proxy socket is bind-mounted in
  from the host — UNIX sockets are not namespace-scoped, so this is the sole
  egress point.
- **Env-proxy bypass**: static binaries or programs that hardcode IP addresses
  and ignore `HTTP_PROXY` get **no network** in allowlist mode.  This is
  fail-closed: by default everything is denied, nothing leaks through.
- **User namespaces** constrain privilege escalation.  Overlay mounts use
  `userxattr` to avoid needing `CAP_SYS_ADMIN` on the host.

**Limitations**: pocketverse is **not** a security boundary against a hostile
kernel exploit.  A sandboxed process that finds a kernel vulnerability can
escape.  Use it to prevent accidental host modifications and to constrain
normal software — not to contain malicious code under an untrusted kernel.

## Command reference

### `pocket run`

```
pocket run [-c CONFIG] [--session ID] [--dry-run] [-- CMD...]
```

Run a sandbox session.  Prints the session ID to stderr before launching.
The command after `--` overrides the config's `command`.  With `--dry-run`,
prints the would-be `unshare` command and exits.

Use `--detach` to keep the sandbox running in a persistent tmux session:

```console
$ pocket run -c CONFIG --detach -- bash
$ pocket attach -c CONFIG --session ID
$ pocket exec -c CONFIG --session ID -- sh -c 'printf hello'
$ pocket detach -c CONFIG --session ID
```

Detached control requires `tmux` on the host and inside the sandbox
userland. `detach` disconnects clients without stopping the sandbox;
`exec` creates a temporary tmux window and returns the command exit status.

### `pocket diff`

```
pocket diff [-c CONFIG] [--session ID|latest]
```

Show changes recorded in a session.  Each line shows a change kind
(`added`, `modified`, `deleted`), mount index, and the path relative to
the mount source.  Empty diffs print `no changes`.

### `pocket apply`

```
pocket apply [-c CONFIG] [--session ID|latest] [--no-backup] [--dry-run]
```

Apply session changes back to the host filesystem.  Prints the diff first,
then merges.  Creates a timestamped backup under the session directory
unless `--no-backup` is given.  Exits 1 if errors occurred during the merge.

### `pocket sessions`

```
pocket sessions [-c CONFIG]
```

List all session IDs under the config's state directory, newest first.

### `pocket ports`

```
pocket ports [-c CONFIG] [--session ID|latest]
```

Show the port forwards recorded for a session as a small table (NAME, HOST,
TARGET).  Prints `no port forwards recorded for this session` when the session
has none.

### `pocket validate`

```
pocket validate [-c CONFIG]
```

Load and validate the config.  Prints a summary on success (`config OK`);
prints readable validation errors and exits 2 on failure.

### `pocket init`

```
pocket init [PATH]
```

Write an annotated example config to PATH (default `pocketverse.yaml`).
Refuses to overwrite an existing file without `--force`.

### `pocket supervisor`

```
pocket supervisor -c CONFIG [--once]
```

Run the spawn loop: consume `spawn_request` messages from the
`supervisor` mailbox, enforce caps, wait on `depends_on`, launch
`pocket run` children, reap into manifests, notify requesters.
`--once` runs a single tick (debug/tests). Requires `shared:`.

### `pocket mailbox`

```
pocket mailbox RECIPIENT -c CONFIG [--offset N] [--tail] [--print-offset]
```

Read messages from a mailbox (non-destructive, cursor-based).
`--tail` follows new messages until Ctrl-C.

### `pocket mailbox-send`

```
pocket mailbox-send RECIPIENT -c CONFIG --type TYPE \
    [--payload JSON | --payload-file FILE | --text STR] [--from NAME]
```

Append a message (sender defaults to `human`). Example — ask the
supervisor for a new agent:

```
pocket mailbox-send supervisor -c pocketverse.yaml --type spawn_request \
  --payload '{"sender":"human","goal":"write tests for state.py"}'
```

## Telemetry and session events

Every session writes durable JSONL events to
`<state_dir>/<name>/session-<id>/events.jsonl`. Events include session
preparation, sandbox start/finish, and shutdown. This local record requires
no extra dependencies.

Optional OpenTelemetry traces/logs can be enabled with:

```yaml
telemetry:
  enabled: true
  service_name: pocketverse
  export_logs: true
  export_traces: true
  export_metrics: false
```

Install the optional dependencies with `pip install -e '.[telemetry]'`.
Configure the OTLP destination using standard variables such as
`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`, and
`OTEL_EXPORTER_OTLP_HEADERS`. A collector at OTLP gRPC `:4317` or HTTP
`:4318` is recommended. If the SDK is unavailable, local JSONL continues to
work.
