"""Tests for pocketverse.state — no root needed, uses tmp_path."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pocketverse.models import Mount, MountMode, SandboxConfig
from pocketverse.state import (
    SESSION_PREFIX,
    WHITEOUT_XATTR,
    OPAQUE_XATTR,
    ApplyResult,
    Change,
    ChangeKind,
    OverlayState,
    Session,
    _classify_upper_entry,
    apply_session,
    diff_session,
    list_sessions,
    load_session,
    new_session,
)

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def cfg(tmp_path: Path) -> SandboxConfig:
    """Minimal config with one OVERLAY mount."""
    src = tmp_path / "source0"
    src.mkdir()
    return SandboxConfig(
        name="test",
        mounts=[Mount(path=src, mode=MountMode.OVERLAY)],
        state_dir=tmp_path / "state",
    )


@pytest.fixture
def cfg_multi(tmp_path: Path) -> SandboxConfig:
    """Config with two OVERLAY mounts."""
    src0 = tmp_path / "source0"
    src0.mkdir()
    src1 = tmp_path / "source1"
    src1.mkdir()
    ro = tmp_path / "ro"
    ro.mkdir()
    return SandboxConfig(
        name="multi",
        mounts=[
            Mount(path=src0, mode=MountMode.OVERLAY),
            Mount(path=src1, mode=MountMode.OVERLAY),
            Mount(path=ro, mode=MountMode.RO),  # not an overlay
        ],
        state_dir=tmp_path / "state",
    )


# ===========================================================================
# new_session
# ===========================================================================


class TestNewSession:
    def test_default_id_creates_layout(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        assert s.root.is_dir()
        assert s.id.startswith("20")  # timestamp-ish
        assert "-" in s.id
        assert s.config_name == "test"

        # sub-directories
        assert (s.root / "sock").is_dir()
        assert (s.root / "logs").is_dir()
        assert (s.root / "upper" / "0").is_dir()
        assert (s.root / "work" / "0").is_dir()
        assert (s.root / "mnt" / "0").is_dir()

        # meta.json
        assert s.meta_path.is_file()
        meta = json.loads(s.meta_path.read_text())
        assert meta["version"] == 1
        assert meta["id"] == s.id
        assert meta["config_name"] == "test"
        assert "created" in meta
        assert meta["overlays"] == [
            {
                "index": 0,
                "source": str(cfg.mounts[0].path),
                "target": str(cfg.mounts[0].resolved_target),
                "upper": str(s.overlays[0].upper),
                "work": str(s.overlays[0].work),
                "mnt": str(s.overlays[0].mnt),
            }
        ]

    def test_explicit_id(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg, session_id="my-custom-id")
        assert s.id == "my-custom-id"
        assert s.root.name == f"{SESSION_PREFIX}my-custom-id"
        assert s.root.is_dir()

        meta = json.loads(s.meta_path.read_text())
        assert meta["id"] == "my-custom-id"

    def test_explicit_id_duplicate_raises(self, cfg: SandboxConfig) -> None:
        new_session(cfg, session_id="dupe")
        with pytest.raises(FileExistsError):
            new_session(cfg, session_id="dupe")

    def test_overlay_state_attributes(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        ol = s.overlays[0]
        assert ol.index == 0
        assert ol.source == cfg.mounts[0].path
        assert ol.upper == s.root / "upper" / "0"
        assert ol.work == s.root / "work" / "0"
        assert ol.mnt == s.root / "mnt" / "0"

    def test_session_properties(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        assert s.meta_path == s.root / "meta.json"
        assert s.sock_dir == s.root / "sock"
        assert s.proxy_sock == s.root / "sock" / "proxy.sock"
        assert s.log_dir == s.root / "logs"

    def test_multi_overlay_mounts(self, cfg_multi: SandboxConfig) -> None:
        """Only OVERLAY mounts create upper/work/mnt dirs."""
        s = new_session(cfg_multi)
        assert len(s.overlays) == 2  # RO mount is skipped
        assert (s.root / "upper" / "0").is_dir()
        assert (s.root / "upper" / "1").is_dir()
        assert not (s.root / "upper" / "2").exists()


# ===========================================================================
# list_sessions  &  load_session
# ===========================================================================


class TestListSessions:
    def test_empty(self, cfg: SandboxConfig) -> None:
        assert list_sessions(cfg) == []

    def test_newest_first(self, cfg: SandboxConfig) -> None:
        ids = []
        for label in ("z-first", "a-second", "m-third"):
            s = new_session(cfg, session_id=label)
            ids.append(s.id)
            # Ensure distinct mtimes
            (s.root / ".touch").write_text("")

        got = list_sessions(cfg)
        # Should be newest first — we just created them, so the last created
        # has the most recent mtime (monotonic clock granularity permitting).
        # Sort by mtime directly to be sure.
        dirs = [
            d for d in (cfg.state_dir / cfg.name).iterdir()
            if d.name.startswith(SESSION_PREFIX)
        ]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        expected = [d.name[len(SESSION_PREFIX):] for d in dirs]
        assert got == expected


class TestLoadSession:
    def test_roundtrip(self, cfg: SandboxConfig) -> None:
        created = new_session(cfg)
        loaded = load_session(cfg, session_id=created.id)
        assert loaded.id == created.id
        assert loaded.root == created.root
        assert loaded.config_name == created.config_name
        assert len(loaded.overlays) == len(created.overlays)
        for lo, co in zip(loaded.overlays, created.overlays):
            assert lo.source == co.source
            assert lo.upper == co.upper
            assert lo.work == co.work
            assert lo.mnt == co.mnt

    def test_latest(self, cfg: SandboxConfig) -> None:
        old = new_session(cfg, session_id="older")
        new = new_session(cfg, session_id="newer")
        loaded = load_session(cfg, "latest")
        assert loaded.id == "newer"

    def test_not_found(self, cfg: SandboxConfig) -> None:
        with pytest.raises(FileNotFoundError):
            load_session(cfg, "nonexistent")

    def test_latest_no_sessions(self, cfg: SandboxConfig) -> None:
        with pytest.raises(FileNotFoundError):
            load_session(cfg, "latest")

    def test_prefixed_id_accepted(self, cfg: SandboxConfig) -> None:
        """Regression: ids listed by `pocket sessions` carry the session-
        prefix; load_session must accept both bare and prefixed forms."""
        created = new_session(cfg, session_id="bothforms")
        bare = load_session(cfg, session_id="bothforms")
        prefixed = load_session(cfg, session_id=f"session-{created.id}")
        assert bare.root == prefixed.root == created.root


# ===========================================================================
# diff_session
# ===========================================================================


class TestDiffSession:
    """We construct upper content manually after new_session."""

    # -- added --------------------------------------------------------------

    def test_added_file(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        upper = s.overlays[0].upper
        (upper / "new.txt").write_text("hello")
        changes = diff_session(s)
        assert changes == [
            Change(kind=ChangeKind.ADDED, relpath=Path("new.txt"), mount_index=0),
        ]

    def test_added_dir(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        upper = s.overlays[0].upper
        (upper / "sub").mkdir()
        (upper / "sub" / "a.txt").write_text("a")
        changes = diff_session(s)
        assert len(changes) == 2
        # sorted by relpath
        assert changes[0] == Change(
            kind=ChangeKind.ADDED, relpath=Path("sub"), mount_index=0,
        )
        assert changes[1] == Change(
            kind=ChangeKind.ADDED, relpath=Path("sub", "a.txt"), mount_index=0,
        )

    def test_added_symlink(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        upper = s.overlays[0].upper
        (upper / "link").symlink_to("/nonexistent")
        changes = diff_session(s)
        assert changes == [
            Change(kind=ChangeKind.ADDED, relpath=Path("link"), mount_index=0),
        ]

    # -- modified -----------------------------------------------------------

    def test_modified_file_different_content(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "f.txt").write_text("original")
        (upper / "f.txt").write_text("modified")
        changes = diff_session(s)
        assert changes == [
            Change(kind=ChangeKind.MODIFIED, relpath=Path("f.txt"), mount_index=0),
        ]

    def test_modified_file_different_mtime(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "f.txt").write_text("same")
        p = upper / "f.txt"
        p.write_text("same")
        # Force different mtime
        os.utime(p, (os.path.getatime(p), os.path.getmtime(p) + 10))
        changes = diff_session(s)
        assert changes == [
            Change(kind=ChangeKind.MODIFIED, relpath=Path("f.txt"), mount_index=0),
        ]

    def test_modified_file_same_content_no_change(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        p = src / "f.txt"
        p.write_text("identical")
        q = upper / "f.txt"
        q.write_text("identical")
        # Force identical mtime so the comparison skips it
        st = os.lstat(p)
        os.utime(q, (st.st_atime, st.st_mtime), follow_symlinks=False)
        changes = diff_session(s)
        assert changes == []

    def test_modified_type_change(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "item").write_text("i am a file")
        (upper / "item").symlink_to("/target")
        changes = diff_session(s)
        assert changes == [
            Change(kind=ChangeKind.MODIFIED, relpath=Path("item"), mount_index=0),
        ]

    def test_modified_symlink_different_target(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "link").symlink_to("/old")
        (upper / "link").symlink_to("/new")
        changes = diff_session(s)
        assert changes == [
            Change(kind=ChangeKind.MODIFIED, relpath=Path("link"), mount_index=0),
        ]

    # -- whiteout -----------------------------------------------------------

    def test_whiteout_xattr(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "gone.txt").write_text("bye")
        whiteout = upper / "gone.txt"
        whiteout.write_text("")
        try:
            os.setxattr(whiteout, WHITEOUT_XATTR, b'')
        except OSError:
            pytest.skip("filesystem does not support user xattrs")
        changes = diff_session(s)
        assert changes == [
            Change(
                kind=ChangeKind.DELETED, relpath=Path("gone.txt"),
                mount_index=0, detail='whiteout',
            ),
        ]

    # -- opaque dir ---------------------------------------------------------

    def test_opaque_dir(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper

        # Source has some content
        (src / "opq").mkdir(parents=True)
        (src / "opq" / "old.txt").write_text("old")
        (src / "opq" / "keep.txt").write_text("keep")

        # Upper replaces the dir wholesale
        upper_opq = upper / "opq"
        upper_opq.mkdir()
        try:
            os.setxattr(upper_opq, OPAQUE_XATTR, b'y')
        except OSError:
            pytest.skip("filesystem does not support user xattrs")

        (upper_opq / "new.txt").write_text("new")
        (upper_opq / "keep.txt").write_text("changed")

        changes = diff_session(s)
        assert len(changes) == 3
        # sorted by relpath: opq (dir), opq/keep.txt, opq/new.txt
        assert changes[0] == Change(
            kind=ChangeKind.MODIFIED, relpath=Path("opq"),
            mount_index=0, detail='opaque-dir',
        )
        # contents inside opaque dir are ADDED (authoritative)
        assert changes[1] == Change(
            kind=ChangeKind.ADDED, relpath=Path("opq", "keep.txt"),
            mount_index=0,
        )
        assert changes[2] == Change(
            kind=ChangeKind.ADDED, relpath=Path("opq", "new.txt"),
            mount_index=0,
        )

    # -- multi-mount sorting ------------------------------------------------

    def test_multi_mount_sorting(self, cfg_multi: SandboxConfig) -> None:
        s = new_session(cfg_multi)
        upper0 = s.overlays[0].upper
        upper1 = s.overlays[1].upper

        (upper0 / "b.txt").write_text("b")
        (upper1 / "a.txt").write_text("a")

        changes = diff_session(s)
        assert len(changes) == 2
        # sorted by (mount_index, relpath)
        assert changes[0] == Change(
            kind=ChangeKind.ADDED, relpath=Path("b.txt"), mount_index=0,
        )
        assert changes[1] == Change(
            kind=ChangeKind.ADDED, relpath=Path("a.txt"), mount_index=1,
        )


# ===========================================================================
# apply_session
# ===========================================================================


class TestApplySession:
    # -- added --------------------------------------------------------------

    def test_added_file(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (upper / "new.txt").write_text("hello")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert (src / "new.txt").read_text() == "hello"

    def test_added_symlink(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (upper / "link").symlink_to("/target")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert (src / "link").is_symlink()
        assert os.readlink(src / "link") == "/target"

    def test_added_dir(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (upper / "sub").mkdir(parents=True)
        (upper / "sub" / "a.txt").write_text("a")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert (src / "sub" / "a.txt").read_text() == "a"

    # -- modified -----------------------------------------------------------

    def test_modified_file(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "f.txt").write_text("original")
        (upper / "f.txt").write_text("modified")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert (src / "f.txt").read_text() == "modified"

    def test_modified_symlink(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "link").symlink_to("/old")
        (upper / "link").symlink_to("/new")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert os.readlink(src / "link") == "/new"

    # -- deleted ------------------------------------------------------------

    def test_deleted_file(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "gone.txt").write_text("bye")
        whiteout = upper / "gone.txt"
        whiteout.write_text("")
        try:
            os.setxattr(whiteout, WHITEOUT_XATTR, b'')
        except OSError:
            pytest.skip("xattrs not supported")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert not (src / "gone.txt").exists()

    def test_deleted_dir(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "f.txt").write_text("f")

        # Whiteout for a directory uses char-device (cannot create) or
        # xattr whiteout with the directory's name in upper.
        whiteout = upper / "sub"
        whiteout.write_text("")
        try:
            os.setxattr(whiteout, WHITEOUT_XATTR, b'')
        except OSError:
            pytest.skip("xattrs not supported")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert not (src / "sub").exists()

    def test_deleted_missing_source(self, cfg: SandboxConfig) -> None:
        """Whiteout for a path that doesn't exist in source — not an error."""
        s = new_session(cfg)
        upper = s.overlays[0].upper
        whiteout = upper / "ghost.txt"
        whiteout.write_text("")
        try:
            os.setxattr(whiteout, WHITEOUT_XATTR, b'')
        except OSError:
            pytest.skip("xattrs not supported")

        result = apply_session(s, backup=False)
        assert result.errors == []

    # -- opaque dir ---------------------------------------------------------

    def test_opaque_dir_apply(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper

        # Source has old content
        (src / "opq").mkdir(parents=True)
        (src / "opq" / "old.txt").write_text("old")
        # Upper replaces wholesale
        upper_opq = upper / "opq"
        upper_opq.mkdir()
        try:
            os.setxattr(upper_opq, OPAQUE_XATTR, b'y')
        except OSError:
            pytest.skip("xattrs not supported")
        (upper_opq / "new.txt").write_text("new")

        result = apply_session(s, backup=False)
        assert result.errors == []
        assert (src / "opq" / "new.txt").read_text() == "new"
        assert not (src / "opq" / "old.txt").exists()

    # -- backup -------------------------------------------------------------

    def test_backup_created(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper

        (src / "keep.txt").write_text("original")
        (upper / "keep.txt").write_text("modified")
        (upper / "added.txt").write_text("new")

        result = apply_session(s, backup=True)
        assert result.errors == []
        assert result.backup_dir is not None
        assert result.backup_dir.is_dir()

        # Backup should contain the original of keep.txt
        backup_keep = result.backup_dir / "0" / "keep.txt"
        assert backup_keep.read_text() == "original"

        # Backup should NOT contain added.txt (it didn't exist in source)
        assert not (result.backup_dir / "0" / "added.txt").exists()

    def test_backup_missing_source(self, cfg: SandboxConfig) -> None:
        """Deleted file that was already missing — backup silently skipped."""
        s = new_session(cfg)
        upper = s.overlays[0].upper
        whiteout = upper / "ghost.txt"
        whiteout.write_text("")
        try:
            os.setxattr(whiteout, WHITEOUT_XATTR, b'')
        except OSError:
            pytest.skip("xattrs not supported")

        result = apply_session(s, backup=True)
        assert result.errors == []
        # Backup dir was created, but nothing was backed up for the ghost
        mount_backup = result.backup_dir / "0" if result.backup_dir else Path("/nonexistent")
        lst = list(mount_backup.iterdir()) if mount_backup.is_dir() else []
        assert len(lst) == 0

    # -- dry_run ------------------------------------------------------------

    def test_dry_run_no_changes(self, cfg: SandboxConfig) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper
        (upper / "new.txt").write_text("hello")
        (src / "keep.txt").write_text("original")
        whiteout = upper / "keep.txt"
        whiteout.write_text("")
        try:
            os.setxattr(whiteout, WHITEOUT_XATTR, b'')
        except OSError:
            pytest.skip("xattrs not supported")

        result = apply_session(s, dry_run=True)
        assert result.backup_dir is None
        # Source untouched
        assert not (src / "new.txt").exists()
        assert (src / "keep.txt").read_text() == "original"
        # Changes reported
        assert len(result.applied) == 2

    # -- symlink escape guard -----------------------------------------------

    def test_symlink_escape_guard(self, cfg: SandboxConfig, tmp_path: Path) -> None:
        s = new_session(cfg)
        src = s.overlays[0].source
        upper = s.overlays[0].upper

        # Source has a symlink pointing outside
        escape_link = src / "escape"
        escape_link.symlink_to(tmp_path)  # outside source root

        # Upper has an entry that would resolve through that symlink.
        # The MODIFIED change for "escape" (type: symlink->dir) will first
        # remove the symlink and create a real directory, so the child
        # path no longer escapes.  The guard is defense-in-depth for cases
        # where we write *through* a pre-existing symlink that was not
        # itself changed.
        escape_upper_dir = upper / "escape"
        escape_upper_dir.mkdir()
        (escape_upper_dir / "outside.txt").write_text("should not appear")

        result = apply_session(s, backup=False)
        # No errors (the symlink gets replaced by a directory first)
        assert not result.errors, f"unexpected errors: {result.errors}"
        # The file should end up *inside* the mount source, not in tmp_path
        assert (src / "escape" / "outside.txt").read_text() == "should not appear"
        assert not (tmp_path / "outside.txt").exists()


# ===========================================================================
# _classify_upper_entry  (helper)
# ===========================================================================


class TestClassifyUpperEntry:
    def test_whiteout_char_device(self) -> None:
        """Char device with rdev 0/0 classified as whiteout (faked stat)."""
        st = MagicMock(spec=os.stat_result)
        st.st_mode = stat.S_IFCHR | 0o644
        st.st_rdev = 0
        kind, extra = _classify_upper_entry("/fake", st)
        assert kind == 'whiteout'
        assert extra is None

    def test_normal_file(self) -> None:
        """Regular file without special attributes."""
        st = MagicMock(spec=os.stat_result)
        st.st_mode = stat.S_IFREG | 0o644
        st.st_size = 100
        st.st_rdev = 0
        kind, extra = _classify_upper_entry("/fake", st)
        assert kind == 'normal'

    def test_normal_dir(self) -> None:
        """Plain directory without opaque xattr."""
        st = MagicMock(spec=os.stat_result)
        st.st_mode = stat.S_IFDIR | 0o755
        kind, extra = _classify_upper_entry("/fake", st)
        assert kind == 'normal'

    def test_xattr_whiteout_real(self, tmp_path: Path) -> None:
        """Zero-size file with whiteout xattr on a real fs."""
        p = tmp_path / ".wh.whiteout"
        p.write_text("")
        try:
            os.setxattr(p, WHITEOUT_XATTR, b'')
        except OSError:
            pytest.skip("filesystem does not support user xattrs")
        st = os.lstat(p)
        kind, extra = _classify_upper_entry(p, st)
        assert kind == 'whiteout'

    def test_xattr_opaque_real(self, tmp_path: Path) -> None:
        """Directory with opaque xattr on a real fs."""
        p = tmp_path / "opq"
        p.mkdir()
        try:
            os.setxattr(p, OPAQUE_XATTR, b'y')
        except OSError:
            pytest.skip("filesystem does not support user xattrs")
        st = os.lstat(p)
        kind, extra = _classify_upper_entry(p, st)
        assert kind == 'opaque-dir'

    def test_zero_size_file_without_xattr_is_normal(self, tmp_path: Path) -> None:
        """Zero-size file that does NOT carry the whiteout xattr."""
        p = tmp_path / "empty"
        p.write_text("")
        st = os.lstat(p)
        kind, extra = _classify_upper_entry(p, st)
        assert kind == 'normal'


# ===========================================================================
# ApplyResult shape
# ===========================================================================


class TestApplyResult:
    def test_defaults(self) -> None:
        r = ApplyResult(applied=[])
        assert r.applied == []
        assert r.backup_dir is None
        assert r.errors == []

    def test_with_values(self) -> None:
        ch = [Change(kind=ChangeKind.ADDED, relpath=Path("x"), mount_index=0)]
        r = ApplyResult(applied=ch, backup_dir=Path("/bkp"), errors=["err"])
        assert r.applied == ch
        assert r.backup_dir == Path("/bkp")
        assert r.errors == ["err"]
