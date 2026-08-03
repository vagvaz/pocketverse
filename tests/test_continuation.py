"""Tests for continuation (continue_from): worktree branch advance + overlay chain."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pocketverse._entry import _overlay_opts
from pocketverse.launcher import (
    _load_continuation,
    _resolve_overlay_continuation,
    _setup_worktrees,
)
from pocketverse.models import Mount, MountMode, SandboxConfig, WorktreeConfig
from pocketverse.state import new_session, record_overlay_updates


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


def wt_cfg(repo: Path, tmp_path: Path) -> SandboxConfig:
    return SandboxConfig(
        name="cont-wt", state_dir=tmp_path / "state",
        mounts=[Mount(path=repo, target=Path("/workspace"),
                      mode=MountMode.OVERLAY, worktree=WorktreeConfig())],
    )


def plain_cfg(repo: Path, tmp_path: Path) -> SandboxConfig:
    return SandboxConfig(
        name="cont-plain", state_dir=tmp_path / "state",
        mounts=[Mount(path=repo, target=Path("/workspace"),
                      mode=MountMode.OVERLAY)],
    )


class TestWorktreeAdvance:
    def test_child_branch_starts_at_parent_tip(self, repo: Path, tmp_path: Path) -> None:
        cfg = wt_cfg(repo, tmp_path)
        parent = new_session(cfg)
        _setup_worktrees(cfg, parent)
        parent_wt = parent.overlays[0].source
        # simulate agent commit on parent branch
        (parent_wt / "parent-file.txt").write_text("from parent\n")
        git(parent_wt, "add", "-A")
        git(parent_wt, "-c", "user.email=a@b.c", "-c", "user.name=a",
            "commit", "-m", "parent-commit")
        parent_branch = json.loads(parent.meta_path.read_text())["overlays"][0]["worktree"]["branch"]

        child = new_session(cfg)
        hints = _load_continuation(cfg, parent.id)
        _setup_worktrees(cfg, child, hints)
        child_branch = json.loads(child.meta_path.read_text())["overlays"][0]["worktree"]["branch"]

        # child branch contains parent's commit
        log = git(repo, "log", "--oneline", child_branch)
        assert "parent-commit" in log
        # child branch tip is parent's tip (no extra commits yet)
        assert git(repo, "rev-parse", child_branch) == git(repo, "rev-parse", parent_branch)


class TestOverlayChain:
    def test_chain_depth_1_to_2(self, repo: Path, tmp_path: Path) -> None:
        cfg = plain_cfg(repo, tmp_path)
        parent = new_session(cfg)
        (parent.overlays[0].upper / "parent-file.txt").write_text("p\n")

        child = new_session(cfg)
        hints = _load_continuation(cfg, parent.id)
        extra = _resolve_overlay_continuation(cfg, child, hints)

        assert extra[0] == [str(parent.overlays[0].upper)]
        meta = json.loads(child.meta_path.read_text())
        assert meta["overlays"][0]["chain_depth"] == 2

    def test_compaction_at_depth_2(self, repo: Path, tmp_path: Path) -> None:
        cfg = plain_cfg(repo, tmp_path)
        parent = new_session(cfg)
        (parent.overlays[0].upper / "parent-file.txt").write_text("p\n")
        # manually set parent chain_depth to 2 (simulating a grandparent chain)
        record_overlay_updates(parent, {0: {"chain_depth": 2}})

        child = new_session(cfg)
        hints = _load_continuation(cfg, parent.id)
        extra = _resolve_overlay_continuation(cfg, child, hints)

        assert extra == {}  # no extra_lowers (compacted)
        meta = json.loads(child.meta_path.read_text())
        assert meta["overlays"][0]["chain_depth"] == 1
        compaction_dir = Path(meta["overlays"][0]["source"])
        assert compaction_dir.exists()
        # compaction contains base + parent upper merged
        assert (compaction_dir / "tracked.txt").read_text() == "base\n"
        assert (compaction_dir / "parent-file.txt").read_text() == "p\n"


class TestOverlayOpts:
    def test_single_lower(self) -> None:
        ov = {"source": "/base", "upper": "/u", "work": "/w"}
        assert _overlay_opts(ov) == "lowerdir=/base,upperdir=/u,workdir=/w,userxattr"

    def test_chained_lowers(self) -> None:
        ov = {"source": "/base", "upper": "/u", "work": "/w",
              "extra_lowers": ["/parent-upper"]}
        assert _overlay_opts(ov) == "lowerdir=/parent-upper:/base,upperdir=/u,workdir=/w,userxattr"
