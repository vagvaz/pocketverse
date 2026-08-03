from __future__ import annotations

import json
from pathlib import Path

from pocketverse.models import SandboxConfig
from pocketverse.sessionctl import session_running
from pocketverse.state import new_session


def test_session_running_true_for_live_pid(tmp_path: Path) -> None:
    cfg = SandboxConfig(name="ctl", state_dir=tmp_path / "state")
    session = new_session(cfg, "ctl-live")
    (session.root / "run.json").write_text(json.dumps({"pid": __import__("os").getpid()}))
    assert session_running(cfg, "ctl-live") is True


def test_session_running_false_for_stale_manifest(tmp_path: Path) -> None:
    cfg = SandboxConfig(name="ctl", state_dir=tmp_path / "state")
    session = new_session(cfg, "ctl-stale")
    (session.root / "run.json").write_text(json.dumps({"pid": 999999999}))
    assert session_running(cfg, "ctl-stale") is False


def test_session_running_false_without_manifest(tmp_path: Path) -> None:
    cfg = SandboxConfig(name="ctl", state_dir=tmp_path / "state")
    new_session(cfg, "ctl-none")
    assert session_running(cfg, "ctl-none") is False
