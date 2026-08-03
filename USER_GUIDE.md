# pocketverse User Guide

A walkthrough for running autonomous AI coding agents in unprivileged
Linux sandboxes — from "hello world" to multi-agent orchestras.

## Setup

```console
$ git clone <pocketverse-repo> && cd pocketverse
$ python3 -m venv .venv && .venv/bin/pip install -e .
$ pip install bubblewrap        # or your distro's package (NixOS: nix profile install nixpkgs#bubblewrap)
$ pocket init                   # writes an annotated pocketverse.yaml
```

Verify it works:

```console
$ pocket run -- sh -c 'echo "hello from inside the sandbox"'
session: 20260803-120000-abc123
hello from inside the sandbox
```

## Scenario 1: Gated workspace (overlay + diff + apply)

You want an agent to work on `~/project` but you don't want it to touch
the real files until you've reviewed its changes.

```yaml
# pocketverse.yaml
name: my-agent
mounts:
  - path: ~/project
    target: /workspace
    mode: overlay          # copy-on-write — changes stay in a session upper
network:
  mode: none               # no internet
workdir: /workspace
```

```console
$ pocket run                                                            # bash
pocket$ echo "new feature" > /workspace/feature.txt                     # (inside sandbox)
pocket$ exit
$ ls ~/project/feature.txt                                              # untouched
$ pocket diff
added    [mount 0] feature.txt
$ pocket apply               # merge into ~/project (backup taken first)
$ ls ~/project/feature.txt
feature.txt
```

**Key rule:** `overlay` for anything you want to review; `ro` for
reference material; `rw` for live shared directories; `shared` for
agent-to-agent exchange (changes are visible immediately).

## Scenario 2: Rate-limited background agents (two URLs)

You let interactive agents burn freely but cap background agents through
a separate API lane (your future proxy at 127.0.0.1:8317).

```yaml
name: bg-agent
mounts:
  - path: ~/project
    target: /workspace
    mode: overlay
llm:
  base_url: http://127.0.0.1:8317   # background agents go here
network:
  mode: allowlist
  allow:
    - api.anthropic.com              # needed if your proxy isn't localhost
env:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
```

No metering, no gateway built by pocketverse — just the URL knob that
lets you route background agents differently from humans. The gateway
(enforcement point) is yours.

## Scenario 3: Agent commits to git (worktree mount)

The agent works on a per-session git worktree and its commits land in the
real repo.

```yaml
name: git-agent
mounts:
  - path: ~/project
    target: /workspace
    mode: overlay
    worktree: {}            # session worktree on branch session-<id>
network:
  mode: none
workdir: /workspace
```

Inside the sandbox, `git commit` writes into the real repo's object store
(the repo's `.git` is rw-bound at its absolute path). After the session:

```console
$ git log session-<id>          # review the agent's commits
$ git merge session-<id>        # merge into your branch
$ pocket diff --session <id>    # any uncommitted leftovers?
$ pocket apply --session <id> --no-backup
$ git worktree prune            # cleanup (optional)
```

## Scenario 4: Multi-agent fan-out / fan-in

One supervisor, many agents:

```yaml
# supervisor.yaml
name: orchestra
shared:
  path: ./shared                # the coordination plane
mounts:
  - path: ~/project
    target: /workspace
    mode: overlay
    worktree: {}
network:
  mode: none
supervisor:
  max_concurrent: 4
  max_fanout_per_parent: 3
agent:
  type: claude-code             # use "claude -p" headless
  max_budget_usd: 5.0
```

**Terminal 1** — start the supervisor:

```console
$ pocket supervisor -c orchestra.yaml
```

**Terminal 2** — launch a planner, then fan out to implementers:

```console
# Planner
$ pocket mailbox-send supervisor -c orchestra.yaml --type spawn_request \
  --payload '{"sender":"human","goal":"plan the feature split"}'

# Wait for it to finish (check the human mailbox)
$ pocket mailbox human -c orchestra.yaml

# Fan-out: three implementers depending on the planner
$ pocket mailbox-send supervisor -c orchestra.yaml --type spawn_request \
  --payload '{"sender":"human","goal":"implement module A",
              "depends_on":["<planner-sid>"]}'
$ pocket mailbox-send supervisor -c orchestra.yaml --type spawn_request \
  --payload '{"sender":"human","goal":"implement module B",
              "depends_on":["<planner-sid>"]}'
$ pocket mailbox-send supervisor -c orchestra.yaml --type spawn_request \
  --payload '{"sender":"human","goal":"implement module C",
              "depends_on":["<planner-sid>"]}'

# Fan-in: a summarizer that continues the planner's conversation
$ pocket mailbox-send supervisor -c orchestra.yaml --type spawn_request \
  --payload '{"sender":"human","goal":"summarize the three implementations",
              "depends_on":["<a-sid>","<b-sid>","<c-sid>"],
              "continue_from":"<planner-sid>"}'

# Follow progress
$ pocket mailbox human -c orchestra.yaml --tail

# See what happened
$ ls shared/sessions/            # manifests for every child
$ cat shared/logs/<sid>.log       # child stdout/stderr
$ git log session-<planner-sid> session-<a-sid> ...
```

## Scenario 5: Human attaches to guide a running agent

Start a persistent detached session:

```console
$ pocket run -c cfg --detach -- claude
session: <session-id>
```

Attach from another terminal:

```console
$ pocket attach -c cfg --session <session-id>
```

Use tmux's normal detach shortcut (`Ctrl-B`, then `D`) or:

```console
$ pocket detach -c cfg --session <session-id>
```

The sandbox continues running after detaching. Run a one-off command in the
same namespaces with:

```console
$ pocket exec -c cfg --session <session-id> -- env
```

Detached sessions require `tmux` on the host and inside the sandbox
userland.

## Scenario 6: Agent continues another agent's work

```console
$ pocket run -c orchestra.yaml --continue-from <parent-sid>
```

- **Worktree mounts**: the child's session branch starts at the parent's
  branch tip (child sees the parent's commits).
- **Plain overlays**: the parent's upper becomes an extra lower layer.
  After two generations, the next continuation **compacts** — merges
  everything into a fresh base.
- **Conversation**: the agent runtime's native resume kicks in
  automatically (claude-code: `--resume <parent-uuid>`,
  opencode: `--session <parent-session-id>`). The parent's `agent_session_id`
  from the manifest makes this happen without any extra config.

## Reference: pocketverse.yaml (complete)

```yaml
name: my-sandbox                  # identifies the config
version: 1

mounts:                           # filesystem composition
  - path: ~/project                 # host path (~ and $VAR expanded)
    target: /workspace              # sandbox path (default: same as path)
    mode: overlay                   # ro | rw | overlay
    worktree:                       # overlay only: back with a git worktree
      repo: ~/project               # git repo (default: mount path)
      branch: auto                  # "auto" = session-<id>

shared:                           # live rw dir across all sandboxes
  path: ./shared
  target: /shared                   # default /shared

ports:                            # publish sandbox ports on host loopback
  - target: 3000
    name: web                       # injects POCKET_PORT_WEB env
    # host: 8080                    # omit = auto-allocate

network:                          # one of full | allowlist | none
  mode: allowlist
  allow:
    - api.anthropic.com
    - "*.openai.com"               # wildcard subdomain
  port: 3128
  allow_ports: [443, 80]

llm:
  base_url: http://127.0.0.1:8317  # sets ANTHROPIC_BASE_URL + OPENAI_BASE_URL

limits:
  cpu: 3600                        # RLIMIT_CPU (seconds)
  memory: 4G                       # RLIMIT_AS
  file_size: 512M                  # RLIMIT_FSIZE
  open_files: 4096                 # RLIMIT_NOFILE
  # processes: 256                 # RLIMIT_NPROC (per-uid caveat)
  core: 0

agent:
  type: claude-code                # shell | claude-code | opencode
  max_budget_usd: 5.0              # claude-code --max-budget-usd

supervisor:                       # spawn-loop settings
  poll_interval: 2.0
  max_concurrent: 4
  max_fanout_per_parent: 3

env:                              # injected into sandbox
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
  TERM: xterm-256color

workdir: /workspace
state_dir: .pocket/state
command: ["bash"]

isolation:                        # namespace toggles
  die_with_parent: true
  new_session: true
  unshare_pid: true
  unshare_ipc: true
  unshare_uts: true
  hostname: pocket
  clearenv: true
  system_dirs: true
  etc_files: true
  bind_nix_store: true            # NixOS: bind /nix/store automatically
```

## CLI commands

| Command | Purpose |
|---|---|
| `pocket run -c cfg [--session ID] [--dry-run] [--continue-from SID] [-- CMD...]` | Launch a sandbox |
| `pocket diff -c cfg [--session ID]` | Show overlay changes |
| `pocket apply -c cfg [--session ID] [--no-backup] [--dry-run]` | Merge overlay into host |
| `pocket sessions -c cfg` | List sessions |
| `pocket ports -c cfg [--session ID]` | Show port mappings |
| `pocket validate -c cfg` | Check config |
| `pocket init [PATH]` | Write an example config |
| `pocket supervisor -c cfg [--once]` | Run the spawn loop |
| `pocket mailbox RECIPIENT -c cfg [--offset N] [--tail]` | Read a mailbox |
| `pocket mailbox-send RECIPIENT -c cfg --type TYPE [--payload JSON|--payload-file F|--text S] [--from NAME]` | Send a message |

## Quick reference: mailbox message types

| Type | Payload | Direction |
|---|---|---|
| `spawn_request` | `{sender, goal, depends_on?, continue_from?, name?, config?}` | anyone → supervisor |
| `done` | `{session, exit_code, goal}` | supervisor → requester |
| `failed` | `{session, exit_code, reason, goal}` | supervisor → requester |
| `progress` | `{text, ...}` | agent → anyone |
| `question` | `{text}` | agent → human / anyone |
| `answer` | `{text, in_reply_to}` | human / anyone → agent |
| `error` | `{reason, in_reply_to}` | supervisor → sender |

## Caveats

- **Allowlist ≠ exfiltration-proof.** The CONNECT proxy tunnels *anything*
  to allowed domains. Keep the list minimal.
- **NixOS.** Works out of the box — `/nix/store`, profile bins, and
  `/etc/static` are bound automatically (`isolation.bind_nix_store`).
  Disable via `bind_nix_store: false` if you want a minimal sandbox.
- **RLIMIT_NPROC** (`limits.processes`) is counted against your real uid
  across the whole desktop — too-low values break forking everywhere.
- **Worktrees.** Explicit branch names can't be checked out in two
  concurrent sessions. Accumulated worktrees need `git worktree prune`.
- **In-sandbox pocketverse CLI** needs `pydantic`+`pyyaml` available to the
  sandbox python (mount a venv or install into the workspace).
