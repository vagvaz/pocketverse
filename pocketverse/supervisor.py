"""Host-side spawn supervisor — turns mailbox spawn_requests into pockets.

Concept (approved architecture): agents and humans drop
MailboxMessage(type='spawn_request', payload=<SpawnRequest fields>) into
the 'supervisor' mailbox on the shared dir. run_supervisor() polls with
a persisted byte cursor (at-least-once), validates payloads into
models.SpawnRequest, enforces caps, resolves depends_on against session
manifests, launches children as `pocket run` subprocesses, and notifies
requesters of done/failed. Agents are EPHEMERAL — the supervisor is the
only long-lived process; continuity is data (manifests + branches).

State under <shared_root>:
    mailbox/supervisor/in.jsonl       # the spawn channel (see mailbox.py)
    mailbox/supervisor/.cursor        # byte offset consumed so far
    requests/<msg-id>.json            # DURABLE spool of consumed requests:
        {"msg_id", "status": "pending"|"launched"|"failed", "request": {...},
         "session": <sid>|null, "reason": str|null}
    sessions/<session-id>.json        # one manifest per launched child
    sessions/<session-id>.yaml        # the fully-resolved child config
    goals/<session-id>.md             # the goal as a file
    logs/<session-id>.log             # child stdout/stderr

Durability: a consumed request is spooled BEFORE the cursor advances, so
a supervisor crash can never silently lose it. Launch is idempotent via
manifest.request_id. On restart, running manifests whose pid is dead are
reaped as failed('orphaned') — their exit codes are unrecoverable; live
ones count toward max_concurrent until they die.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from . import agents, mailbox
from .models import AgentType, SandboxConfig, SpawnRequest, load_config

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers (testable without processes)
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_sid() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_manifests(sessions_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not sessions_dir.is_dir():
        return out
    for p in sessions_dir.glob("*.json"):
        try:
            m = json.loads(p.read_text())
            if isinstance(m, dict) and "id" in m:
                out[m["id"]] = m
        except (ValueError, OSError) as exc:
            _log.warning("skipping malformed manifest %s: %s", p, exc)
    return out


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def _fanout_exceeded(manifests: dict[str, dict], sender: str, cap: int) -> bool:
    return sum(1 for m in manifests.values() if m.get("parent") == sender) >= cap


def _deps_status(manifests: dict[str, dict], depends_on: list[str]) -> str:
    """'failed' if any dep failed, 'ready' if all done, else 'waiting'."""
    for dep in depends_on:
        if manifests.get(dep, {}).get("status") == "failed":
            return "failed"
    if all(manifests.get(dep, {}).get("status") == "done" for dep in depends_on):
        return "ready"
    return "waiting"


def _build_child_config(cfg: SandboxConfig, req: SpawnRequest, sid: str) -> SandboxConfig:
    if req.config is None:
        child = cfg.model_copy(deep=True)
    elif isinstance(req.config, dict):
        child = SandboxConfig.model_validate(req.config)
    else:
        child = load_config(req.config)
    child.name = req.name or f"{cfg.name}-{sid[:8]}"
    shared_target = str(child.shared.target) if child.shared else "/shared"
    child.env = {
        **child.env,
        "POCKET_GOAL": req.goal,
        "POCKET_GOAL_FILE": f"{shared_target}/goals/{sid}.md",
        "POCKET_SESSION_ID": sid,
        "POCKET_SENDER": req.sender,
        **({"POCKET_CONTINUE_FROM": req.continue_from} if req.continue_from else {}),
    }
    return child


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class _Supervisor:
    def __init__(self, cfg: SandboxConfig, popen: Callable[..., Any]) -> None:
        assert cfg.shared is not None
        self.cfg = cfg
        self.sup = cfg.supervisor
        self.shared = cfg.shared.path
        self.sessions_dir = self.shared / "sessions"
        self.requests_dir = self.shared / "requests"
        self.goals_dir = self.shared / "goals"
        self.logs_dir = self.shared / "logs"
        self.cursor_path = self.shared / "mailbox" / "supervisor" / ".cursor"
        self.popen = popen
        self.running: dict[str, dict[str, Any]] = {}  # sid -> {proc, log_fh, manifest}
        self._stop = False

    # -- cursor -------------------------------------------------------------
    def _load_cursor(self) -> int:
        try:
            return int(self.cursor_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _store_cursor(self, offset: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_suffix(".tmp")
        tmp.write_text(str(offset))
        tmp.rename(self.cursor_path)

    # -- request spool --------------------------------------------------------
    def _spool_path(self, msg_id: str) -> Path:
        return self.requests_dir / f"{msg_id}.json"

    def _load_pending(self) -> list[tuple[SpawnRequest, str, Path]]:
        out = []
        if not self.requests_dir.is_dir():
            return out
        for p in sorted(self.requests_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text())
                if d.get("status") != "pending":
                    continue
                out.append((SpawnRequest.model_validate(d["request"]), d["msg_id"], p))
            except Exception as exc:
                _log.warning("skipping malformed spool %s: %s", p, exc)
        return out

    def _mark_spool(self, path: Path, status: str, **extra: Any) -> None:
        try:
            d = json.loads(path.read_text())
        except (ValueError, OSError):
            return
        d["status"] = status
        d.update(extra)
        _write_json_atomic(path, d)

    # -- tick phases ----------------------------------------------------------
    def _consume(self) -> None:
        cursor = self._load_cursor()
        messages, new_cursor = mailbox.read(self.shared, "supervisor", offset=cursor)
        for msg in messages:
            if msg.type != "spawn_request":
                continue
            if self._spool_path(msg.id).exists():
                continue  # idempotent under cursor replay
            try:
                req = SpawnRequest.model_validate(msg.payload)
            except Exception as exc:
                mailbox.append(self.shared, msg.sender, "error",
                               {"reason": str(exc), "in_reply_to": msg.id},
                               sender="supervisor")
                continue
            _write_json_atomic(self._spool_path(msg.id), {
                "msg_id": msg.id, "status": "pending",
                "request": req.model_dump(mode="json"),
                "session": None, "reason": None,
            })
        if new_cursor != cursor:
            self._store_cursor(new_cursor)

    def _fail_request(self, path: Path, req: SpawnRequest, reason: str) -> None:
        self._mark_spool(path, "failed", reason=reason)
        mailbox.append(self.shared, req.sender, "failed",
                       {"reason": reason, "goal": req.goal},
                       sender="supervisor")

    def _process_pending(self, manifests: dict[str, dict]) -> None:
        for req, msg_id, path in self._load_pending():
            # idempotent launch: a manifest already carrying this request id?
            if any(m.get("request_id") == msg_id for m in manifests.values()):
                self._mark_spool(path, "launched")
                continue
            if _fanout_exceeded(manifests, req.sender, self.sup.max_fanout_per_parent):
                self._fail_request(path, req, "fanout cap reached")
                continue
            status = _deps_status(manifests, req.depends_on)
            if status == "failed":
                self._fail_request(path, req, "dependency failed")
                continue
            if status == "waiting":
                continue
            running_count = sum(1 for m in manifests.values()
                                if m.get("status") == "running")
            if running_count >= self.sup.max_concurrent:
                continue
            try:
                m = self._launch(req, msg_id, manifests)
                manifests[m["id"]] = m
                self._mark_spool(path, "launched", session=m["id"])
            except Exception as exc:
                _log.exception("launch failed for %s", msg_id)
                self._fail_request(path, req, f"launch error: {exc}")

    def _launch(self, req: SpawnRequest, msg_id: str,
                manifests: dict[str, dict]) -> dict:
        sid = _new_sid()
        child = _build_child_config(self.cfg, req, sid)

        # -- agent runtime: build the headless agent command + native id ----
        agent_type = child.agent.type
        agent_session_id: str | None = None
        resume_native_id: str | None = None
        agent_cmd: list[str] | None = None
        if agent_type is not AgentType.SHELL:
            if req.continue_from:
                parent = manifests.get(req.continue_from, {})
                if parent.get("agent_type") == agent_type.value and \
                        parent.get("agent_session_id"):
                    resume_native_id = parent["agent_session_id"]
                else:
                    _log.warning(
                        "continue_from %s: no matching native session id "
                        "(manifest missing, type mismatch, or id uncaptured)",
                        req.continue_from)
            agent_session_id = (
                str(uuid.uuid4()) if agent_type is AgentType.CLAUDE else None
            )
            agent_cmd = agents.build_command(
                child, req.goal,
                session_id=agent_session_id,
                resume_native_id=resume_native_id,
                interactive=False,
            )

        self.goals_dir.mkdir(parents=True, exist_ok=True)
        (self.goals_dir / f"{sid}.md").write_text(req.goal)
        child_yaml = self.sessions_dir / f"{sid}.yaml"
        child_yaml.parent.mkdir(parents=True, exist_ok=True)
        child_yaml.write_text(yaml.safe_dump(child.model_dump(mode="json")))
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": sid,
            "request_id": msg_id,
            "parent": req.sender,
            "goal": req.goal,
            "name": child.name,
            "status": "running",
            "pid": None,
            "exit_code": None,
            "started_at": _now(),
            "finished_at": None,
            "continue_from": req.continue_from,
            "config_path": str(child_yaml),
            "agent_type": agent_type.value,
            "agent_session_id": agent_session_id,
        }
        log_fh = open(self.logs_dir / f"{sid}.log", "ab")
        argv = [sys.executable, "-m", "pocketverse.cli", "run", "-c",
                str(child_yaml), "--session", sid]
        if req.continue_from:
            argv += ["--continue-from", req.continue_from]
        if agent_cmd is not None:
            argv += ["--", *agent_cmd]
        proc = self.popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        manifest["pid"] = getattr(proc, "pid", None)
        _write_json_atomic(self.sessions_dir / f"{sid}.json", manifest)
        self.running[sid] = {"proc": proc, "log_fh": log_fh, "manifest": manifest}
        _log.info("launched %s (parent=%s)", sid, req.sender)
        return manifest

    def _reap(self, manifests: dict[str, dict]) -> None:
        for sid in list(self.running):
            entry = self.running[sid]
            rc = entry["proc"].poll()
            if rc is None:
                continue
            entry["log_fh"].close()
            m = entry["manifest"]
            m["status"] = "done" if rc == 0 else "failed"
            m["exit_code"] = rc
            m["finished_at"] = _now()
            if m.get("agent_type") == AgentType.OPENCODE.value and \
                    not m.get("agent_session_id"):
                captured = agents.capture_native_id(
                    AgentType.OPENCODE, self.logs_dir / f"{sid}.log")
                if captured:
                    m["agent_session_id"] = captured
            _write_json_atomic(self.sessions_dir / f"{sid}.json", m)
            manifests[sid] = m
            mailbox.append(self.shared, m["parent"], m["status"],
                           {"session": sid, "exit_code": rc, "goal": m["goal"]},
                           sender="supervisor")
            del self.running[sid]
            _log.info("reaped %s rc=%s", sid, rc)

    def _sweep_orphans(self, manifests: dict[str, dict]) -> None:
        """Running manifests we don't own whose pid is dead -> failed(orphaned).

        Exit codes of adopted children are unrecoverable after a supervisor
        restart, so they are conservatively marked failed; live ones keep
        counting toward max_concurrent via their manifest status.
        """
        for sid, m in manifests.items():
            if m.get("status") != "running" or sid in self.running:
                continue
            if _pid_alive(m.get("pid")):
                continue
            m["status"] = "failed"
            m["finished_at"] = _now()
            _write_json_atomic(self.sessions_dir / f"{sid}.json", m)
            mailbox.append(self.shared, m.get("parent", "human"), "failed",
                           {"session": sid,
                            "reason": "orphaned: supervisor restarted, exit code lost",
                            "goal": m.get("goal")},
                           sender="supervisor")
            _log.warning("reaped orphaned session %s", sid)

    def tick(self) -> None:
        manifests = _read_manifests(self.sessions_dir)
        self._sweep_orphans(manifests)
        self._consume()
        self._reap(manifests)      # free capacity first…
        self._process_pending(manifests)  # …so freed slots launch this tick

    def shutdown(self) -> None:
        for sid, entry in list(self.running.items()):
            entry["proc"].terminate()
        deadline = time.monotonic() + 5.0
        for sid, entry in list(self.running.items()):
            proc = entry["proc"]
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                proc.kill()
            rc = proc.poll()
            entry["log_fh"].close()
            m = entry["manifest"]
            m["status"] = "failed"
            m["exit_code"] = rc
            m["finished_at"] = _now()
            _write_json_atomic(self.sessions_dir / f"{sid}.json", m)
            del self.running[sid]


def run_supervisor(
    cfg: SandboxConfig,
    *,
    once: bool = False,
    _popen: Callable[..., Any] = subprocess.Popen,
) -> int:
    if cfg.shared is None:
        raise ValueError("supervisor needs shared: configured")
    sup = _Supervisor(cfg, _popen)
    if once:
        sup.tick()
        return 0

    def _handle(signum: int, frame: Any) -> None:
        sup._stop = True

    try:
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
    except ValueError:
        pass  # not in main thread (tests)

    _log.info("supervisor polling %s every %.1fs", sup.shared, sup.sup.poll_interval)
    while not sup._stop:
        try:
            sup.tick()
        except Exception:
            _log.exception("tick failed; continuing")
        time.sleep(sup.sup.poll_interval)
    sup.shutdown()
    return 130
