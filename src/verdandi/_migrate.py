"""Adopt what this tool kept under its old name, scrobd.

Called first thing by every command. It moves `<base>/scrobd` into `<base>/verdandi`, for the
config, data and state bases alike, and does nothing once there is no old directory left.

**Why a merge and not a rename of the directory.** The mpv script runs `mkdir -p` on the new
state directory and points `watch.stderr` into it before Python starts, so the new directory
already exists on the very first run. Each file is moved on its own instead.

**Never overwrites, never forks.** A file is moved with a hard link, which refuses if the
destination exists, so two watchers starting together cannot both win. Where the destination
already has the name, an empty old file is dropped (nothing in it), and anything with content
is kept beside it as `<name>.scrobd`, so no session row is lost and the old directory can go.
`sessions.jsonl` is what matters: the parallel run against trakt-scrobbler reads one log, and a
second one would be a gap in it.
"""

import contextlib
import os
import sys
from pathlib import Path

from . import _log, paths

_OLD_APP = "scrobd"

# The log file carried the app's name; nothing else did.
_RENAMED = {"scrobd.jsonl": "verdandi.jsonl"}


def _adopt(old: Path, new: Path) -> tuple[list[str], list[str]]:
    """Move *old*'s files into *new*; return (moved, kept-beside-with-a-suffix)."""
    new.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    kept: list[str] = []
    for entry in sorted(old.iterdir()):
        dest = new / _RENAMED.get(entry.name, entry.name)
        try:
            os.link(entry, dest)
            entry.unlink()
            moved.append(dest.name)
            continue
        except FileExistsError:
            pass
        except OSError:
            continue  # lost a race to another watcher, or not a plain file: leave it
        if entry.stat().st_size == 0:
            entry.unlink(missing_ok=True)
            continue
        beside = new / f"{dest.name}.{_OLD_APP}"
        try:
            os.link(entry, beside)
            entry.unlink()
            kept.append(beside.name)
        except OSError:
            continue  # it stays in the old directory, which stays, and the next run retries
    with contextlib.suppress(OSError):
        old.rmdir()  # not empty means something stayed behind; the next run retries
    return moved, kept


def adopt_old_name() -> None:
    """Move scrobd's config, data and state directories to verdandi's; log what moved."""
    done: list[tuple[Path, Path, list[str], list[str]]] = []
    for base in paths.roots():
        old = base / _OLD_APP
        if not old.is_dir():
            continue
        new = base / paths.APP
        moved, kept = _adopt(old, new)
        done.append((old, new, moved, kept))
    for old, new, moved, kept in done:
        if not (moved or kept):
            continue
        _log.event("migrated", source=str(old), target=str(new), moved=moved, kept_beside=kept)
        if kept:
            sys.stderr.write(
                f"verdandi: {old} held {', '.join(kept)} that the new directory already "
                f"had; kept beside them with a .{_OLD_APP} suffix\n"
            )
