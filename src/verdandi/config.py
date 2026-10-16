"""verdandi's config file, `config.toml` in the XDG config directory.

It holds two settings: the library, the folders `verdandi watch` records, and
the server each session is pushed to.

```toml
# Only files under these folders are watched -- the trakt-scrobbler whitelist model.
library = [
    "/mnt/DezLegion2021/Documents/Media/TV_Shows",
    "/mnt/DezLegion2021/Documents/Media/Movies",
]
# Where `verdandi watch` and `verdandi push` send the session log. Without it,
# nothing is pushed. The token it needs is a file of its own, `token`, beside this one.
server = "https://urd.example"
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
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .paths import config_dir


def path() -> Path:
    """Return the config file's path, whether or not it exists."""
    return config_dir() / "config.toml"


def _warn(message: str) -> None:
    """Put one line on stderr: for `verdandi watch`, the `watch.stderr` the owner reads."""
    sys.stderr.write(f"verdandi: {message}\n")


@dataclass(frozen=True)
class Settings:
    """Everything the config file says, read once."""

    library: list[str] | None
    server: str | None


def load() -> Settings:
    """Read the config file once: the library, and the server to push to."""
    table = _table()
    if table is None:
        return Settings(library=None, server=None)
    return Settings(library=_library(table), server=_server(table))


def library_roots() -> list[str] | None:
    """Return the library's root folders, normalised, or None for no library: record everything.

    An entry that is not an absolute folder is skipped with a line on stderr;
    a root that resolved against whichever directory mpv was started in would
    be a different library every time. If none is left, there is no library.
    """
    return load().library


def server_url() -> str | None:
    """Return the server sessions are pushed to, without a trailing `/`, or None to push nowhere."""
    return load().server


def _table() -> dict[str, object] | None:
    """Return the config file parsed, or None if there is none or it cannot be read."""
    file = path()
    try:
        with file.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn(f"cannot read {file}, so no library or server is set: {exc}")
        return None
    # `tomllib` decodes the bytes itself and lets a UnicodeDecodeError through.
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        _warn(f"{file} is not valid TOML, so no library or server is set: {exc}")
        return None


def _library(table: dict[str, object]) -> list[str] | None:
    """Return the library's roots from the parsed *table*; see `library_roots`."""
    entries = table.get("library", [])
    if not isinstance(entries, list):
        # Not iterated: a string's characters include `/`, a root holding everything.
        _warn(
            f"{path()}: `library` must be a list of folders, not {type(entries).__name__}, "
            "so no library is set"
        )
        return None
    roots = []
    for entry in entries:
        root = _absolute_folder(entry)
        if root is None:
            _warn(f"{path()}: library entry {entry!r} is not an absolute folder, so it is skipped")
            continue
        roots.append(root)
    return roots or None


def _server(table: dict[str, object]) -> str | None:
    """Return the server from the parsed *table*, or None if it is absent or unusable.

    Checked here because `push` hands it to urllib, which would as happily open a
    `file://` URL as a web address. A URL carrying a user or password is refused
    without being repeated: that part may be a secret, and the token has a file of
    its own. So is one urllib cannot take apart -- an unclosed `[`, a port that is
    not a number, no host -- which would otherwise fail on every push instead of once here.
    """
    value = table.get("server")
    if value is None:
        return None
    # First, before any warning that repeats the value: an `@` anywhere means a URL
    # that may carry a password, whether or not the rest of it parses.
    if isinstance(value, str) and "@" in value:
        _warn(
            f"{path()}: `server` must not carry a user or password -- the token goes in "
            "the `token` file beside it -- so nothing is pushed"
        )
        return None
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        _warn(f"{path()}: `server` must be an http(s) URL, not {value!r}, so nothing is pushed")
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        usable = bool(parts.hostname) and parts.port != 0
    except ValueError:  # urlsplit on an unclosed `[`, `.port` on one that is not a number
        usable = False
    if not usable:
        _warn(f"{path()}: `server` {value!r} is not a URL urllib can use, so nothing is pushed")
        return None
    return value.rstrip("/")


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
