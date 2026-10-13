"""Things the resolver could not answer. Never sent, never lost.

Read fresh from disk on every call, for the same reason `aliases` is: the daemon
and the `review` command are separate processes, and a cached table would let one
miss the other's writes.

Per-series, not per-episode: a twelve-episode season you binged asks one
question, not twelve. `seen` counts how many files hit it, which is the honest
measure of how much a single answer is worth.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict, TypeGuard

from ._store import JsonObject, read_table, write_atomic
from .paths import state_dir

# Sonarr's own folder formats: seasonFolderFormat is "Season {season:00}" and
# specialsFolderFormat is "Specials". Match those exactly. A startswith("season")
# prefix swallowed real titles -- "Season of the Witch (2011)" is a film, not a
# season folder -- and missed "Specials", which collided every show's specials.
_SEASON_DIR = re.compile(r"season\s*\d+|specials", re.IGNORECASE)


class Entry(TypedDict):
    """One series waiting on an answer: the shape `add` writes."""

    folder: str
    title: str
    example: str
    seen: int


def _file() -> Path:
    return state_dir() / "review.json"


def _load() -> JsonObject:
    return read_table(_file(), {})


def _save(table: Mapping[str, object]) -> None:
    write_atomic(_file(), table)  # atomic: the review command may be reading


def _folder_key(path: str) -> str:
    """Return the folder a queue entry keys on: the series, never one of its seasons."""
    parent = Path(path).parent
    return parent.parent.name if _SEASON_DIR.fullmatch(parent.name) else parent.name


def _is_row(entry: object) -> TypeGuard[Entry]:
    """Whether a value read back off disk is an entry this module wrote.

    A row that is not one is skipped everywhere rather than raised on: a queue
    the `review` command cannot open is a queue whose contents are lost, which
    is the same failure as never queuing the file. `count` and `pending` agree
    on this predicate so the count can never promise a row the listing will not
    show -- `len()` of the raw table once counted the three elements of a `[]`
    that had been written over the file.
    """
    if not isinstance(entry, dict):
        return False
    text = (entry.get("folder"), entry.get("title"), entry.get("example"))
    seen = entry.get("seen")
    return (
        all(isinstance(t, str) for t in text)
        and isinstance(seen, int)
        and not isinstance(seen, bool)
    )


def add(path: str, title: str) -> Entry:
    """Record one file the resolver could not identify, keyed by series folder.

    Returns the entry as it now stands. A caller that logs the queueing needs
    the folder key the file landed under and how many files are now waiting on
    that one answer, and reading the table back to learn them would be a second
    disk read for facts this call already has in hand.
    """
    table = _load()
    folder = _folder_key(path)
    entry = table.get(folder)
    if not _is_row(entry):
        entry = Entry(folder=folder, title=title, example=path, seen=0)
        table[folder] = entry  # a malformed row is replaced, not accumulated onto
    entry["seen"] += 1
    _save(table)
    return entry


def pending() -> list[Entry]:
    """Return queued entries, most-repeated question first."""
    return sorted((e for e in _load().values() if _is_row(e)), key=lambda e: -e["seen"])


def count() -> int:
    """Return how many series are currently queued."""
    return sum(1 for e in _load().values() if _is_row(e))


def resolve_entry(folder: str) -> bool:
    """Remove *folder* from the queue; return whether a queued entry was actually removed.

    The caller needs the answer. `scrobd review --answer "Mystery Show "` matched
    nothing on the trailing space and still reported success, leaving the entry
    queued forever and an alias under a key no lookup will ever produce. Keys are
    matched literally -- a typo is reported, never guessed at.
    """
    table = _load()
    if not _is_row(table.get(folder)):
        return False  # nothing `pending()` would have listed
    del table[folder]
    _save(table)
    return True
