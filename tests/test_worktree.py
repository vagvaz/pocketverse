"""Tests for worktree mounts (overlay backed by per-session git worktree)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from pocketverse.launcher import _setup_worktrees
from pocketverse.models import Mount, MountMode, SandboxConfig, WorktreeConfig
from pocketverse.state import (
    ChangeKind,
    apply_session,
    diff_session,
    load_session,
    new_session,
)


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
    (repo / "tracked.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


def make_cfg(repo: Path, tmp_path: Path, wt: WorktreeConfig | None = None) -> SandboxConfig:
    return SandboxConfig(
        name="wt-test",
        state_dir=tmp_path / "state",
        mounts=[Mount(path=repo, target=Path("/workspace"),
                      mode=MountMode.OVERLAY,
                      worktree=wt if wt is not None else WorktreeConfig())],
    )


class TestValidation:
    def test_worktree_requires_overlay(self, repo: Path) -> None:
        with pytest.raises(ValidationError, match="overlay"):
            Mount(path=repo, mode=MountMode.RW, worktree=WorktreeConfig())

    def test_plain_mounts_untouched(self, repo: Path, tmp_path: Path) -> None:
        cfg = SandboxConfig(
            name="plain", state_dir=tmp_path / "s2",
            mounts=[Mount(path=repo, mode=MountMode.OVERLAY)])
        sess = new_session(cfg)
        _setup_worktrees(cfg, sess)
        assert sess.overlays[0].source == repo  # unchanged


class TestSetup:
    def test_creates_worktree_and_updates_session(self, repo: Path, tmp_path: Path) -> None:
        cfg = make_cfg(repo, tmp_path)
        sess = new_session(cfg)
        _setup_worktrees(cfg, sess)

        meta = json.loads(sess.meta_path.read_text())
        wt_meta = meta["overlays"][0]["worktree"]
        wt_path = Path(wt_meta["path"])
        branch = wt_meta["branch"]

        assert branch == f"session-{sess.id}"
        assert (wt_path / "tracked.txt").read_text() == "one\n"
        assert branch in git(repo, "branch", "--list")
        # overlay source redirected, in meta and in memory
        assert meta["overlays"][0]["source"] == str(wt_path)
        assert sess.overlays[0].source == wt_path
        # gitdir rw-bound for in-sandbox commits
        assert any(m.path == repo / ".git" and m.mode is MountMode.RW
                   for m in cfg.mounts)
        # load_session sees the redirected source (diff/apply target worktree)
        loaded = load_session(cfg, sess.id)
        assert loaded.overlays[0].source == wt_path

    def test_explicit_branch_created(self, repo: Path, tmp_path: Path) -> None:
        cfg = make_cfg(repo, tmp_path, WorktreeConfig(branch="feature-x"))
        sess = new_session(cfg)
        _setup_worktrees(cfg, sess)
        assert "feature-x" in git(repo, "branch", "--list")

    def test_not_a_repo(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        cfg = make_cfg(plain, tmp_path)
        sess = new_session(cfg)
        with pytest.raises(RuntimeError, match="not a git repository"):
            _setup_worktrees(cfg, sess)


class TestDiffApplyTargetsWorktree:
    def test_roundtrip(self, repo: Path, tmp_path: Path) -> None:
        cfg = make_cfg(repo, tmp_path)
        sess = new_session(cfg)
        _setup_worktrees(cfg, sess)
        wt_path = sess.overlays[0].source

        # simulate agent changes in the overlay upper
        upper = sess.overlays[0].upper
        (upper / "newfile.txt").write_text("agent made this\n")

        changes = diff_session(sess)
        assert [(c.kind, str(c.relpath)) for c in changes] == [
            (ChangeKind.ADDED, "newfile.txt")]

        result = apply_session(sess, backup=False)
        assert not result.errors
        assert (wt_path / "newfile.txt").read_text() == "agent made this\n"
        # original repo checkout untouched
        assert not (repo / "newfile.txt").exists()
