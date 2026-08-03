"""Tests for pocketverse.cli — subprocess-free, uses capsys + tmp_path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from pocketverse.cli import main, _EXAMPLE_CONFIG
from pocketverse.models import Mount, MountMode, SandboxConfig
from pocketverse.state import new_session, SESSION_PREFIX


# ===========================================================================
# validate
# ===========================================================================

class TestValidate:
    def test_ok(self, tmp_path: Path, capsys) -> None:
        """Valid config prints a summary and exits 0."""
        src = (tmp_path / "src").resolve()
        src.mkdir()
        state_dir = (tmp_path / "state").resolve()
        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(yaml.dump({
            "name": "test-agent",
            "mounts": [
                {"path": str(src), "mode": "overlay"},
                {"path": str(src / "ref"), "mode": "ro"},
            ],
            "network": {"mode": "allowlist", "allow": ["api.example.com"]},
            "env": {"KEY": "${KEY}"},
            "state_dir": str(state_dir),
        }))
        (src / "ref").mkdir()

        ret = main(["validate", "-c", str(cfg_path)])
        assert ret == 0
        out = capsys.readouterr().out
        assert "name: test-agent" in out
        assert "mounts: 2" in out
        assert "overlay" in out
        assert "ro" in out
        assert "allowlist" in out
        assert "1 rules" in out
        assert "1 env var" in out
        assert "config OK" in out

    def test_bad(self, tmp_path: Path, capsys) -> None:
        """Invalid config (allowlist without rules) exits 2."""
        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(yaml.dump({
            "name": "bad",
            "network": {"mode": "allowlist", "allow": []},
            "state_dir": str((tmp_path / "state").resolve()),
        }))

        ret = main(["validate", "-c", str(cfg_path)])
        assert ret == 2
        err = capsys.readouterr().err
        assert "validation error" in err.lower() or "error" in err.lower()

    def test_bad_yaml(self, tmp_path: Path, capsys) -> None:
        """Truly invalid YAML exits 2."""
        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(": [ broken yaml ")

        ret = main(["validate", "-c", str(cfg_path)])
        assert ret == 2
        err = capsys.readouterr().err
        assert "error" in err.lower()


# ===========================================================================
# init
# ===========================================================================

class TestInit:
    def test_writes_file(self, tmp_path: Path, capsys) -> None:
        """init writes the example config to the given path."""
        target = tmp_path / "pocketverse.yaml"
        ret = main(["init", str(target)])
        assert ret == 0
        assert target.exists()
        content = target.read_text()
        assert "pocketverse.yaml" in content
        assert "annotated example" in content
        assert content == _EXAMPLE_CONFIG

    def test_refuses_overwrite(self, tmp_path: Path, capsys) -> None:
        """init without --force refuses to overwrite."""
        target = tmp_path / "pocketverse.yaml"
        target.write_text("existing")
        ret = main(["init", str(target)])
        assert ret == 2
        err = capsys.readouterr().err
        assert "already exists" in err
        assert target.read_text() == "existing"  # untouched

    def test_force_overwrite(self, tmp_path: Path, capsys) -> None:
        """init with --force overwrites an existing file."""
        target = tmp_path / "pocketverse.yaml"
        target.write_text("existing")
        ret = main(["init", "--force", str(target)])
        assert ret == 0
        assert target.exists()
        assert "annotated example" in target.read_text()

    def test_default_path(self, tmp_path: Path, capsys) -> None:
        """init with no path defaults to pocketverse.yaml in CWD."""
        old_cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            ret = main(["init"])
            assert ret == 0
            target = tmp_path / "pocketverse.yaml"
            assert target.exists()
            assert "annotated example" in target.read_text()
        finally:
            os.chdir(old_cwd)


# ===========================================================================
# sessions
# ===========================================================================

class TestSessions:
    def test_empty(self, tmp_path: Path, capsys) -> None:
        """sessions on empty state produces no output."""
        state_dir = (tmp_path / "state").resolve()
        state_dir.mkdir(parents=True)
        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(yaml.dump({
            "name": "test",
            "state_dir": str(state_dir),
        }))

        ret = main(["sessions", "-c", str(cfg_path)])
        assert ret == 0
        out = capsys.readouterr().out
        assert out.strip() == ""


# ===========================================================================
# diff
# ===========================================================================

class TestDiff:
    def test_synthetic_session(self, tmp_path: Path, capsys) -> None:
        """CLI diff on a session with an added file shows the change."""
        src = (tmp_path / "source").resolve()
        src.mkdir()
        state_dir = (tmp_path / "state").resolve()

        # Create config and session programmatically
        cfg = SandboxConfig(
            name="test",
            mounts=[Mount(path=src, mode=MountMode.OVERLAY)],
            state_dir=state_dir,
        )
        sess = new_session(cfg, session_id="test-session")

        # Add a file to the upper directory
        (sess.overlays[0].upper / "new.txt").write_text("hello")

        # Write equivalent YAML for the CLI
        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(yaml.dump({
            "name": "test",
            "mounts": [{"path": str(src), "mode": "overlay"}],
            "state_dir": str(state_dir),
        }))

        ret = main(["diff", "-c", str(cfg_path), "--session", "test-session"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "added" in out
        assert "[mount 0]" in out
        assert "new.txt" in out
        assert "summary: 1 change" in out

    def test_no_changes(self, tmp_path: Path, capsys) -> None:
        """Empty diff prints 'no changes'."""
        src = (tmp_path / "source").resolve()
        src.mkdir()
        state_dir = (tmp_path / "state").resolve()

        cfg = SandboxConfig(
            name="test",
            mounts=[Mount(path=src, mode=MountMode.OVERLAY)],
            state_dir=state_dir,
        )
        new_session(cfg, session_id="empty-session")

        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(yaml.dump({
            "name": "test",
            "mounts": [{"path": str(src), "mode": "overlay"}],
            "state_dir": str(state_dir),
        }))

        ret = main(["diff", "-c", str(cfg_path), "--session", "empty-session"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "no changes" in out

    def test_latest(self, tmp_path: Path, capsys) -> None:
        """diff --session latest finds the most recent session."""
        src = (tmp_path / "source").resolve()
        src.mkdir()
        state_dir = (tmp_path / "state").resolve()

        cfg = SandboxConfig(
            name="test",
            mounts=[Mount(path=src, mode=MountMode.OVERLAY)],
            state_dir=state_dir,
        )
        # Create two sessions — the second is newer
        new_session(cfg, session_id="older")
        sess2 = new_session(cfg, session_id="newer")
        (sess2.overlays[0].upper / "file.txt").write_text("data")

        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(yaml.dump({
            "name": "test",
            "mounts": [{"path": str(src), "mode": "overlay"}],
            "state_dir": str(state_dir),
        }))

        ret = main(["diff", "-c", str(cfg_path), "--session", "latest"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "file.txt" in out


# ===========================================================================
# apply (dry-run only — no root needed)
# ===========================================================================

class TestApply:
    def test_dry_run(self, tmp_path: Path, capsys) -> None:
        """apply --dry-run shows changes without touching source."""
        src = (tmp_path / "source").resolve()
        src.mkdir()
        state_dir = (tmp_path / "state").resolve()

        cfg = SandboxConfig(
            name="test",
            mounts=[Mount(path=src, mode=MountMode.OVERLAY)],
            state_dir=state_dir,
        )
        sess = new_session(cfg, session_id="apply-test")
        (sess.overlays[0].upper / "new.txt").write_text("hello")

        cfg_path = tmp_path / "pocketverse.yaml"
        cfg_path.write_text(yaml.dump({
            "name": "test",
            "mounts": [{"path": str(src), "mode": "overlay"}],
            "state_dir": str(state_dir),
        }))

        ret = main(["apply", "-c", str(cfg_path), "--session", "apply-test",
                     "--dry-run", "--no-backup"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "added" in out
        assert "new.txt" in out
        # Source should not have the file (dry-run)
        assert not (src / "new.txt").exists()
