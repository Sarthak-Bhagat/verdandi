"""A local mirror of what Sonarr and Radarr already know.

Asking Sonarr at play time is a network round trip for a fact it wrote weeks
ago. The index is rebuilt on Sonarr's import webhook instead, so a lookup at
play time is a dict hit.

Keyed on the folder, because every episode of a series shares one and the
filename may have been renamed by hand.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from ._store import JsonObject, read_table, write_atomic
from .paths import data_dir
from .resolution import UNKNOWN, Ids, Resolution, ids_from
from .tier0 import SE_RE, absolute_from, ancestors


class Row(TypedDict):
    """One indexed series or film, keyed by its folder's name."""

    ids: Ids
    title: str


Index = dict[str, dict[str, Row]]
"""`{"series": {folder: Row}, "movies": {folder: Row}}`, as `build` writes it."""


def _id(value: object) -> int | str | None:
    """Return *value* if it is a usable id from the *arr's JSON, else None."""
    if isinstance(value, bool) or not isinstance(value, int | str) or not value:
        return None
    return value


def _title(value: object) -> str:
    return value if isinstance(value, str) else ""


def build(series: list[JsonObject], movies: list[JsonObject]) -> Index:
    """Build the index dict from already-parsed Sonarr/Radarr JSON."""
    out: Index = {"series": {}, "movies": {}}
    for s in series:
        path, tvdb = s.get("path"), _id(s.get("tvdbId"))
        if not isinstance(path, str) or not path or tvdb is None:
            continue
        ids: Ids = {"tvdb": tvdb}
        if (imdb := _id(s.get("imdbId"))) is not None:
            ids["imdb"] = imdb
        out["series"][Path(path).name] = {"ids": ids, "title": _title(s.get("title"))}
    for m in movies:
        path = m.get("path")
        if not isinstance(path, str) or not path:
            continue
        ids = {}
        if (imdb := _id(m.get("imdbId"))) is not None:
            ids["imdb"] = imdb
        if (tmdb := _id(m.get("tmdbId"))) is not None:
            ids["tmdb"] = tmdb
        if not ids:
            continue
        out["movies"][Path(path).name] = {"ids": ids, "title": _title(m.get("title"))}
    return out


def _file() -> Path:
    return data_dir() / "index.json"


def save(idx: Mapping[str, object]) -> Path:
    """Write the index to disk atomically and return the path written."""
    p = _file()
    write_atomic(p, idx)    # atomic: the daemon may be reading
    return p


def load() -> JsonObject:
    """Read the index from disk, degrading to empty on missing or corrupt data."""
    return read_table(_file(), {"series": {}, "movies": {}})


def _row(idx: Mapping[str, object], section: str, part: str) -> tuple[Ids, str] | None:
    """One well-formed row of *section* as `(ids, title)`, or None.

    The file on disk is whatever was last written there, so neither the section
    nor the row is assumed to be the shape `build` emits. `ids` is checked
    strictly because it is the answer; `title` is display-only and defaulted.
    A bad row is skipped, not raised on -- one corrupt series must not make
    every other series in the index unresolvable.
    """
    rows = idx.get(section)
    entry = rows.get(part) if isinstance(rows, dict) else None
    if not isinstance(entry, dict):
        return None
    ids = ids_from(entry.get("ids"))
    return None if ids is None else (ids, _title(entry.get("title")))


def lookup(idx: Mapping[str, object], path: str) -> Resolution:
    """Identify a file by walking its ancestor directories against the index."""
    stem = Path(path).stem
    for part in ancestors(path):
        hit = _row(idx, "series", part)
        if hit is not None:
            se = SE_RE.search(stem)
            if not se:
                return UNKNOWN      # the index names the series, not the episode
            ids, title = hit
            return Resolution(kind="episode", ids=ids,
                              season=int(se.group(1)), episode=int(se.group(2)),
                              absolute=absolute_from(stem),
                              title=title, confidence="exact", source="index")
        hit = _row(idx, "movies", part)
        if hit is not None:
            ids, title = hit
            return Resolution(kind="movie", ids=ids, season=None,
                              episode=None, absolute=None, title=title,
                              confidence="exact", source="index")
    return UNKNOWN
