"""scrobd's config file, `config.toml` in the XDG config directory.

It holds one setting so far: the library, the folders `scrobd watch` records.

```toml
# Only files under these folders are watched -- the trakt-scrobbler whitelist model.
library = [
    "/mnt/DezLegion2021/Documents/Media/TV_Shows",
    "/mnt/DezLegion2021/Documents/Media/Movies",
]
```

Every mpv starts a watcher, so without a library YouTube, music and clips each
become a session and, unidentified, a question in the review queue. A folder
allowlist was chosen (2026-10-02) over starting the watcher only from mediactl,
which would miss a library file opened from Dolphin.

**A broken config never means "record nothing".** A missing file, or a missing
or empty `library`, is *unscoped*: every local file is recorded, as before there
was a config. A file that cannot be read or parsed is unscoped too, and says so
on stderr. Losing watches to a typo is worse than recording a few extra rows.

**Strings only.** A root is normalised by expanding `~` and folding `.` and
`..`, never by `resolve` or anything else that asks the filesystem: the library
lives on an autofs/sshfs mount that can hang in D state, and a check that
touched it would freeze the watcher with it.
"""

import os
import sys
import tomllib
from pathlib import Path

from .paths import config_dir


def path() -> Path:
    """Return the config file's path, whether or not it exists."""
    return config_dir() / "config.toml"


def _warn(message: str) -> None:
    """Put one line on stderr: for `scrobd watch`, the `watch.stderr` the owner reads."""
    sys.stderr.write(f"scrobd: {message}\n")


def library_roots() -> list[str] | None:
    """Return the library's root folders, normalised, or None for no library: record everything.

    An entry that is not an absolute folder is skipped with a line on stderr;
    a root that resolved against whichever directory mpv was started in would
    be a different library every time. If none is left, there is no library.
    """
    file = path()
    try:
        with file.open("rb") as f:
            table = tomllib.load(f)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn(f"cannot read {file}, so no library is set: {exc}")
        return None
    # `tomllib` decodes the bytes itself and lets a UnicodeDecodeError through.
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        _warn(f"{file} is not valid TOML, so no library is set: {exc}")
        return None
    entries = table.get("library", [])
    if not isinstance(entries, list):
        # Not iterated: a string's characters include `/`, a root holding everything.
        _warn(
            f"{file}: `library` must be a list of folders, not {type(entries).__name__}, "
            "so no library is set"
        )
        return None
    roots = []
    for entry in entries:
        root = _absolute_folder(entry)
        if root is None:
            _warn(f"{file}: library entry {entry!r} is not an absolute folder, so it is skipped")
            continue
        roots.append(root)
    return roots or None


def _absolute_folder(entry: object) -> str | None:
    """Return *entry* with `~` expanded and `.` and `..` folded, or None if it is not absolute.

    `Path.expanduser` raises where `os.path.expanduser` would leave `~someone`
    unexpanded, which is a relative path and skipped all the same.
    """
    if not isinstance(entry, str):
        return None
    try:
        folder = Path(entry).expanduser()
    except RuntimeError:  # `~someone` with no such user, or no home at all
        return None
    # `normpath`, not `resolve`: it folds `..` in the string and never asks the mount.
    return os.path.normpath(folder) if folder.is_absolute() else None
