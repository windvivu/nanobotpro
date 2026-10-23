"""Which code this process runs, and whether the checkout on disk has moved on (custom).

`git pull` updates the files, but a running bot keeps the Python it imported when it started (the
templates, read from disk for each page, do change). Seen live: a dashboard restart that never
happened, a page claiming "Gateway is back online", and the bot still on code from months before.

So the process records the checkout's commit when it starts, and a boot id that is new on every start
(an in-place restart through os.execv keeps the PID, not this id). The dashboard header shows the
commit and warns when the checkout's HEAD differs; /healthz returns the boot id, so the restart page
knows a new process answers. Commits are read from .git directly: no git binary is needed, and an
install without a checkout (a Docker image) simply has no commit to show.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

# nanobot/web/code_version.py -> the checkout the package was imported from
REPO_ROOT = Path(__file__).resolve().parents[2]
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def _is_sha(value: str) -> bool:
    return bool(_SHA.fullmatch(value))


def _git_dirs(root: Path) -> tuple[Path, Path] | None:
    """(the checkout's git dir, the dir holding shared refs); a worktree's .git is a file."""
    dot = root / ".git"
    if dot.is_dir():
        git_dir = dot
    elif dot.is_file():
        text = dot.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        git_dir = Path(text[len("gitdir:"):].strip())
        git_dir = git_dir if git_dir.is_absolute() else (root / git_dir)
        if not git_dir.is_dir():
            return None
    else:
        return None
    common = git_dir
    commondir = git_dir / "commondir"
    if commondir.is_file():
        shared = Path(commondir.read_text(encoding="utf-8").strip())
        common = shared if shared.is_absolute() else (git_dir / shared)
    return git_dir, common


def disk_commit(root: Path = REPO_ROOT) -> str | None:
    """The checkout's HEAD commit now, or None outside a checkout or when it cannot be read."""
    try:
        dirs = _git_dirs(root)
        if dirs is None:
            return None
        git_dir, common = dirs
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head if _is_sha(head) else None  # detached HEAD
        ref = head[len("ref:"):].strip()
        for base in (git_dir, common):
            loose = base / ref
            if loose.is_file():
                sha = loose.read_text(encoding="utf-8").strip()
                return sha if _is_sha(sha) else None
        for base in (git_dir, common):
            packed = base / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    sha, _, name = line.partition(" ")
                    if name.strip() == ref and _is_sha(sha):
                        return sha
    except (OSError, UnicodeError):
        return None
    return None


# Set once, when the dashboard module is first imported at start-up: the code this process loaded
RUNNING_COMMIT: str | None = disk_commit()
BOOT_ID = uuid.uuid4().hex[:16]
STARTED_AT = time.time()


def status(root: Path = REPO_ROOT) -> dict:
    """What runs and what is on disk now; stale when a pull (or a checkout) changed the commit."""
    disk = disk_commit(root)
    return {
        "running": RUNNING_COMMIT,
        "disk": disk,
        "stale": bool(RUNNING_COMMIT and disk and disk != RUNNING_COMMIT),
        "boot": BOOT_ID,
        "started_at": STARTED_AT,
    }
