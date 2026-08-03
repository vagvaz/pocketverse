"""Session state: overlay upper/work dirs, metadata, diff and apply.

Ownership: Lane 3 (@fixer). Implement every function below to spec.
Do not edit models.py; import from it.

Layout created per session (under cfg.state_dir / cfg.name / session-<id>):

    root/
      meta.json            # config snapshot + mount bookkeeping (see SESSION META below)
      mnt/<i>/             # empty mountpoints where overlays get mounted by _entry
      upper/<i>/           # overlayfs upperdir for overlay mount <i>
      work/<i>/            # overlayfs workdir for overlay mount <i>
      sock/                # directory containing proxy.sock (allowlist mode)
      logs/                # proxy + launcher logs
      backup-<ts>/         # created by apply_session(backup=True)

SESSION META (meta.json):
    {
      "version": 1,
      "id": "20260726-153000-ab12cd",
      "config_name": cfg.name,
      "created": "<iso8601>",
      "overlays": [
        {"index": <int position in cfg.mounts>, "source": "<host path>",
         "target": "<sandbox path>", "upper": "<abs>", "work": "<abs>", "mnt": "<abs>"}
      ]
    }

OVERLAYFS DIFF SEMANTICS (kernel 5.11+ unprivileged, mounted with -o userxattr):
  * A deleted lower file appears in upper as EITHER
      (a) a character device with rdev 0/0, OR
      (b) a zero-size regular file carrying the xattr 'user.overlay.whiteout'.
  * An "opaque" directory (replaced wholesale) carries xattr
    'user.overlay.opaque' == b'y'. For diff purposes treat its contents as
    authoritative (everything under it is ADDED/MODIFIED); for apply, the
    lower directory must be replaced, not merged.
  * Otherwise an upper entry is ADDED (absent in lower) or MODIFIED.
  * Compare with the lower (mount source) using os.lstat on both; consider
    a regular file MODIFIED if size/mtime/content differ (cheap check:
    size or mtime differs is enough; do not hash).
"""

from __future__ import annotations

import enum
import json
import os
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import SandboxConfig

SESSION_PREFIX = "session-"
WHITEOUT_XATTR = "user.overlay.whiteout"
OPAQUE_XATTR = "user.overlay.opaque"


class ChangeKind(str, enum.Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass
class Change:
    kind: ChangeKind
    relpath: Path          # path relative to the mount source
    mount_index: int       # index into cfg.mounts
    detail: str = ""       # e.g. "whiteout", "opaque-dir", human-readable notes


@dataclass
class OverlayState:
    index: int             # position in cfg.mounts
    source: Path
    target: Path
    upper: Path
    work: Path
    mnt: Path


@dataclass
class Session:
    id: str
    root: Path
    config_name: str
    overlays: list[OverlayState] = field(default_factory=list)

    @property
    def meta_path(self) -> Path:
        return self.root / "meta.json"

    @property
    def sock_dir(self) -> Path:
        return self.root / "sock"

    @property
    def proxy_sock(self) -> Path:
        return self.sock_dir / "proxy.sock"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"


@dataclass
class ApplyResult:
    applied: list[Change]
    backup_dir: Path | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _session_dir(cfg: SandboxConfig, session_id: str) -> Path:
    """Return the on-disk root directory for *session_id* (without *SESSION_PREFIX*)."""
    return cfg.state_dir / cfg.name / f"{SESSION_PREFIX}{session_id}"


def _session_id_from_dir(d: Path) -> str:
    """Strip the *SESSION_PREFIX* from a session directory name."""
    name = d.name
    if name.startswith(SESSION_PREFIX):
        return name[len(SESSION_PREFIX):]
    return name


def _generate_session_id() -> str:
    """Produce a unique session id: ``<UTC %Y%m%d-%H%M%S>-<6 lowercase hex>``."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_hex(3)  # 6 lowercase hex chars
    return f"{ts}-{rand}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_upper_entry(path: str | Path, st: os.stat_result) -> tuple[str, str | None]:
    """Classify an upper filesystem entry.

    Returns ``(kind, extra)`` where *kind* is one of:

    - ``'whiteout'``   – a deleted-file marker
    - ``'opaque-dir'`` – a directory that replaces the lower wholesale
    - ``'normal'``     – regular entry to compare against the source

    *extra* is always ``None`` for now.
    """
    # (a) character device with major/minor 0/0
    if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
        return ('whiteout', None)

    # (b) zero-size regular file with whiteout xattr
    if stat.S_ISREG(st.st_mode) and st.st_size == 0:
        try:
            os.getxattr(path, WHITEOUT_XATTR)
            return ('whiteout', None)
        except OSError:
            pass

    # opaque directory
    if stat.S_ISDIR(st.st_mode):
        try:
            if os.getxattr(path, OPAQUE_XATTR) == b'y':
                return ('opaque-dir', None)
        except OSError:
            pass

    return ('normal', None)


def _compare_entries(
    upper_path: str | Path,
    source_path: str | Path,
    st_upper: os.stat_result | None = None,
) -> bool:
    """Return ``True`` if the upper entry differs from the source entry.

    The caller guarantees that *upper_path* exists. *source_path* may be
    missing (returns ``True`` when it is).  *st_upper* may be passed to
    avoid a redundant ``lstat``.
    """
    try:
        st_src = os.lstat(source_path)
    except FileNotFoundError:
        return True

    if st_upper is None:
        try:
            st_upper = os.lstat(upper_path)
        except OSError:
            return True

    # Type differs (S_IFMT mask)
    if stat.S_IFMT(st_upper.st_mode) != stat.S_IFMT(st_src.st_mode):
        return True

    # Regular file: size or mtime differs
    if stat.S_ISREG(st_upper.st_mode):
        return st_upper.st_size != st_src.st_size or st_upper.st_mtime != st_src.st_mtime

    # Symlink: target differs
    if stat.S_ISLNK(st_upper.st_mode):
        try:
            return os.readlink(upper_path) != os.readlink(source_path)
        except OSError:
            return True

    # Directories (and everything else): no per-entry comparison beyond type
    return False


def _inside_opaque(relpath: Path, opaque_dirs: set[Path]) -> bool:
    """Return ``True`` when *relpath* lies inside an opaque directory."""
    return any(p in opaque_dirs for p in relpath.parents)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def new_session(cfg: SandboxConfig, session_id: str | None = None) -> Session:
    """Create a fresh session directory tree for `cfg` and return it.

    - session_id None -> generate f"{SESSION_PREFIX}<UTC %Y%m%d-%H%M%S>-<6 hex>".
    - Creates root, mnt/<i>, upper/<i>, work/<i> per OVERLAY mount (i = index
      in cfg.mounts), sock/, logs/.
    - Writes meta.json per the layout documented in the module docstring.
    - Refuses (FileExistsError) if the session id already exists.
    """
    if session_id is None:
        session_id = _generate_session_id()

    root = _session_dir(cfg, session_id)

    if root.exists():
        raise FileExistsError(f"Session directory already exists: {root}")

    # Create directory structure --------------------------------------------------
    root.mkdir(parents=True, exist_ok=True)
    (root / "sock").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)

    overlays: list[OverlayState] = []
    for i, mount in enumerate(cfg.overlay_mounts()):
        upper_dir = root / "upper" / str(i)
        work_dir = root / "work" / str(i)
        mnt_dir = root / "mnt" / str(i)

        upper_dir.mkdir(parents=True)
        work_dir.mkdir(parents=True)
        mnt_dir.mkdir(parents=True)

        overlays.append(
            OverlayState(
                index=i,
                source=mount.path,
                target=mount.resolved_target,
                upper=upper_dir,
                work=work_dir,
                mnt=mnt_dir,
            )
        )

    # Write meta.json ------------------------------------------------------------
    created = _now_iso()
    meta = {
        "version": 1,
        "id": session_id,
        "config_name": cfg.name,
        "created": created,
        "overlays": [
            {
                "index": o.index,
                "source": str(o.source),
                "target": str(o.target),
                "upper": str(o.upper),
                "work": str(o.work),
                "mnt": str(o.mnt),
            }
            for o in overlays
        ],
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2))

    return Session(
        id=session_id,
        root=root,
        config_name=cfg.name,
        overlays=overlays,
    )


def load_session(cfg: SandboxConfig, session_id: str = "latest") -> Session:
    """Load an existing session from disk.

    - session_id "latest" -> the most recently modified session dir.
    - Raises FileNotFoundError if absent / no sessions.
    - Reconstructs OverlayState list from meta.json.
    """
    sessions_dir = cfg.state_dir / cfg.name

    if session_id == "latest":
        if not sessions_dir.is_dir():
            raise FileNotFoundError(f"No sessions directory: {sessions_dir}")
        dirs = sorted(
            [d for d in sessions_dir.iterdir()
             if d.is_dir() and d.name.startswith(SESSION_PREFIX)],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if not dirs:
            raise FileNotFoundError(f"No sessions found in {sessions_dir}")
        session_dir = dirs[0]
        sid = _session_id_from_dir(session_dir)
    else:
        # Accept both bare ids (as printed by `pocket run`) and prefixed
        # dir names (as listed by `pocket sessions`).
        dirname = session_id if session_id.startswith(SESSION_PREFIX) else f"{SESSION_PREFIX}{session_id}"
        session_dir = sessions_dir / dirname
        if not session_dir.is_dir():
            raise FileNotFoundError(f"Session directory not found: {session_dir}")
        sid = session_id

    meta_path = session_dir / "meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"meta.json not found in {session_dir}")

    meta = json.loads(meta_path.read_text())

    overlays = []
    for od in meta.get("overlays", []):
        overlays.append(
            OverlayState(
                index=od["index"],
                source=Path(od["source"]),
                target=Path(od["target"]),
                upper=Path(od["upper"]),
                work=Path(od["work"]),
                mnt=Path(od["mnt"]),
            )
        )

    return Session(
        id=sid,
        root=session_dir,
        config_name=meta.get("config_name", cfg.name),
        overlays=overlays,
    )


def list_sessions(cfg: SandboxConfig) -> list[str]:
    """Return all session ids for cfg.name, newest first."""
    sessions_dir = cfg.state_dir / cfg.name
    if not sessions_dir.is_dir():
        return []

    dirs = sorted(
        [d for d in sessions_dir.iterdir()
         if d.is_dir() and d.name.startswith(SESSION_PREFIX)],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return [_session_id_from_dir(d) for d in dirs]


def record_overlay_updates(session: Session, updates: dict[int, dict]) -> None:
    """Merge per-overlay metadata into meta.json and the in-memory session.

    updates maps overlay index -> a dict merged into the overlay's meta
    entry. The key "source" additionally syncs OverlayState.source in
    memory (used by the worktree redirect and continuation compaction).
    """
    meta = json.loads(session.meta_path.read_text())
    for ov in meta.get("overlays", []):
        u = updates.get(ov["index"])
        if u:
            ov.update(u)
    session.meta_path.write_text(json.dumps(meta, indent=2))
    for o in session.overlays:
        u = updates.get(o.index)
        if u and "source" in u:
            o.source = Path(u["source"])


# Backwards-compatible alias (worktree setup).
def record_worktrees(session: Session, updates: dict[int, dict]) -> None:
    record_overlay_updates(session, updates)


def diff_session(session: Session) -> list[Change]:
    """Compute the change set of every overlay upper vs its lower (source).

    Follow the OVERLAYFS DIFF SEMANTICS in the module docstring. Entries
    under an opaque directory are reported relative to it (the opaque dir
    itself yields one Change with detail='opaque-dir'; still recurse and
    report the contents as ADDED/MODIFIED). Skip internal overlayfs artifacts
    (any path component named 'work' is impossible here; but skip nothing
    else silently). Sort by (mount_index, relpath) for stable output.
    """
    changes: list[Change] = []

    for overlay in session.overlays:
        upper = overlay.upper
        source = overlay.source

        if not upper.is_dir():
            continue

        opaque_dirs: set[Path] = set()

        for dirpath, dirnames, filenames in os.walk(upper, followlinks=False):
            rel_dir = Path(dirpath).relative_to(upper)
            if str(rel_dir) == '.':
                rel_dir = Path('')

            # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
            # Sub-directories
            # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
            for dname in list(dirnames):
                dpath = os.path.join(dirpath, dname)
                try:
                    st = os.lstat(dpath)
                except OSError:
                    continue

                d_relpath = rel_dir / dname
                kind, _ = _classify_upper_entry(dpath, st)

                if kind == 'whiteout':
                    changes.append(Change(
                        kind=ChangeKind.DELETED,
                        relpath=d_relpath,
                        mount_index=overlay.index,
                        detail='whiteout',
                    ))
                    dirnames.remove(dname)  # do not recurse

                elif kind == 'opaque-dir':
                    changes.append(Change(
                        kind=ChangeKind.MODIFIED,
                        relpath=d_relpath,
                        mount_index=overlay.index,
                        detail='opaque-dir',
                    ))
                    opaque_dirs.add(d_relpath)
                    # os.walk will recurse; children are handled below

                elif _inside_opaque(d_relpath, opaque_dirs):
                    # Child of an opaque dir → everything is ADDED
                    changes.append(Change(
                        kind=ChangeKind.ADDED,
                        relpath=d_relpath,
                        mount_index=overlay.index,
                    ))

                else:
                    # Normal directory – compare with source
                    source_path = source / d_relpath
                    try:
                        st_src = os.lstat(source_path)
                        if not stat.S_ISDIR(st_src.st_mode):
                            # Type conflict → MODIFIED
                            changes.append(Change(
                                kind=ChangeKind.MODIFIED,
                                relpath=d_relpath,
                                mount_index=overlay.index,
                            ))
                    except FileNotFoundError:
                        changes.append(Change(
                            kind=ChangeKind.ADDED,
                            relpath=d_relpath,
                            mount_index=overlay.index,
                        ))

            # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
            # Files & symlinks
            # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(fpath)
                except OSError:
                    continue

                f_relpath = rel_dir / fname
                kind, _ = _classify_upper_entry(fpath, st)

                if kind == 'whiteout':
                    changes.append(Change(
                        kind=ChangeKind.DELETED,
                        relpath=f_relpath,
                        mount_index=overlay.index,
                        detail='whiteout',
                    ))

                elif kind == 'opaque-dir':
                    # A regular file with the opaque xattr (very unusual)
                    changes.append(Change(
                        kind=ChangeKind.MODIFIED,
                        relpath=f_relpath,
                        mount_index=overlay.index,
                        detail='opaque-dir',
                    ))

                elif _inside_opaque(f_relpath, opaque_dirs):
                    # Inside an opaque dir → ADDED (old content discarded)
                    changes.append(Change(
                        kind=ChangeKind.ADDED,
                        relpath=f_relpath,
                        mount_index=overlay.index,
                    ))

                else:
                    source_path = source / f_relpath
                    try:
                        os.lstat(source_path)
                        exists = True
                    except FileNotFoundError:
                        exists = False

                    if not exists:
                        changes.append(Change(
                            kind=ChangeKind.ADDED,
                            relpath=f_relpath,
                            mount_index=overlay.index,
                        ))
                    else:
                        if _compare_entries(fpath, source_path, st):
                            changes.append(Change(
                                kind=ChangeKind.MODIFIED,
                                relpath=f_relpath,
                                mount_index=overlay.index,
                            ))

    changes.sort(key=lambda c: (c.mount_index, c.relpath))
    return changes


# ---------------------------------------------------------------------------
# Apply helpers
# ---------------------------------------------------------------------------


def _safe_remove(path: Path) -> None:
    """Remove *path* (file, symlink, or directory).  Missing is not an error."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_entry(src: Path, dst: Path) -> None:
    """Copy an entry (file, symlink, directory) from *src* to *dst*.

    Metadata (mode, mtime) is preserved where possible.  Symlinks are
    created as symlinks, never followed.
    """
    st = os.lstat(src)

    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(src)
        dst.symlink_to(target)
        try:
            os.lutimes(dst, (st.st_atime, st.st_mtime))
        except (OSError, AttributeError):
            pass
    elif stat.S_ISDIR(st.st_mode):
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copystat(src, dst, follow_symlinks=False)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def _backup_entry(src: Path, dst: Path) -> None:
    """Copy *src* into *dst* for backup purposes.

    Directories are deep-copied; symlinks are preserved.
    """
    st = os.lstat(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def _make_backup_dir(session: Session) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = session.root / f"backup-{ts}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    return backup_dir


# ---------------------------------------------------------------------------
# Public apply API
# ---------------------------------------------------------------------------


def apply_session(
    session: Session,
    *,
    backup: bool = True,
    dry_run: bool = False,
) -> ApplyResult:
    """Merge the overlay uppers back into their source directories.

    - ADDED/MODIFIED: copy file/symlink/dir from upper to source, preserving
      mode and mtimes (shutil.copystat semantics). Symlinks are copied as
      symlinks.
    - DELETED (whiteout): remove the corresponding path in source
      (file or dir; missing path is not an error, note it in detail).
    - Opaque dirs: replace the lower directory wholesale (delete then copy).
    - backup=True: before touching anything, copy every source path that
      will be modified or deleted into root/backup-<UTC ts>/ preserving
      relative layout per mount_index; set ApplyResult.backup_dir.
    - dry_run=True: compute and return `applied` without touching the
      filesystem (backup_dir stays None).
    - Never follow symlinks pointing outside the mount source.
    - Errors (permissions etc.) are collected into ApplyResult.errors;
      continue past them rather than raising mid-merge.
    """
    errors: list[str] = []

    # Compute the change set -------------------------------------------------
    try:
        changes = diff_session(session)
    except Exception as e:
        errors.append(f"Failed to compute diff: {e}")
        return ApplyResult(applied=[], backup_dir=None, errors=errors)

    if dry_run:
        return ApplyResult(applied=changes, backup_dir=None, errors=errors)

    # Prepare backup ---------------------------------------------------------
    backup_dir: Path | None = None
    if backup:
        backup_dir = _make_backup_dir(session)

    # Apply each change ------------------------------------------------------
    for change in changes:
        overlay = session.overlays[change.mount_index]
        source_root = overlay.source
        upper_root = overlay.upper
        source_path = source_root / change.relpath
        source_real = source_root.resolve()

        # Guard: never follow symlinks escaping the mount source
        try:
            parent_real = source_path.parent.resolve()
        except OSError as e:
            errors.append(f"[{change.relpath}] cannot resolve parent: {e}")
            continue

        if (
            not str(parent_real).startswith(str(source_real) + os.sep)
            and parent_real != source_real
        ):
            errors.append(
                f"[{change.relpath}] resolved path {parent_real} "
                f"escapes mount source {source_real}"
            )
            continue

        # Backup (before mutating) -------------------------------------------
        if backup_dir is not None:
            _backup_path(backup_dir, change.mount_index, change.relpath, source_root)

        # Perform the change --------------------------------------------------
        try:
            if change.kind == ChangeKind.ADDED:
                upper_path = upper_root / change.relpath
                try:
                    os.lstat(upper_path)
                except FileNotFoundError:
                    errors.append(f"[{change.relpath}] upper entry missing for ADDED")
                    continue
                source_path.parent.mkdir(parents=True, exist_ok=True)
                _copy_entry(upper_path, source_path)

            elif change.kind == ChangeKind.MODIFIED:
                if change.detail == 'opaque-dir':
                    upper_path = upper_root / change.relpath
                    _safe_remove(source_path)
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(upper_path, source_path, symlinks=True)
                else:
                    upper_path = upper_root / change.relpath
                    try:
                        os.lstat(upper_path)
                    except FileNotFoundError:
                        errors.append(f"[{change.relpath}] upper entry missing for MODIFIED")
                        continue
                    _safe_remove(source_path)
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    _copy_entry(upper_path, source_path)

            elif change.kind == ChangeKind.DELETED:
                if not source_path.exists() and not source_path.is_symlink():
                    try:
                        os.lstat(source_path)
                    except FileNotFoundError:
                        change.detail = f"{change.detail}; source path already missing" if change.detail else "source path already missing"
                _safe_remove(source_path)

        except OSError as e:
            errors.append(f"[{change.relpath}] {e}")
            continue

    return ApplyResult(applied=changes, backup_dir=backup_dir, errors=errors)


def _backup_path(backup_dir: Path, mount_index: int, relpath: Path, src_root: Path) -> None:
    """Copy a source path to the backup location.  Missing source is silently skipped."""
    src = src_root / relpath
    try:
        os.lstat(src)
    except FileNotFoundError:
        return

    dst = backup_dir / str(mount_index) / relpath
    _backup_entry(src, dst)
