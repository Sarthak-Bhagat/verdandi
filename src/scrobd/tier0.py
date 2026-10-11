"""Read the identity straight out of the path. No I/O, no guessing.

Sonarr and Radarr write the id into both the folder and the filename, so this
works for a file that has been moved out of its folder -- which is every file
mediactl trashes, since its trash flattens the hierarchy.

Tag forms seen in this library, all accepted:
    [tvdbid-466998]   Sonarr, current
    [imdbid-tt16428256]  Radarr, current
    [imdb-tt9646546]     Sonarr, pre-2026-09-20; still on archived copies
An empty tag -- [imdb-] -- is not an id. That was a real fault here: Sonarr's
{ImdbId} token renders empty for a series with no imdb entry, while {TvdbId}
is an int and always renders.

`os.path.normpath` below is string arithmetic, not I/O: it never opens, stats
or resolves anything. The "no I/O" promise holds.
"""

import os
import re
from pathlib import Path

from .resolution import UNKNOWN, Ids, Resolution

_ID = re.compile(r"\[(tvdbid|imdbid|tmdbid|imdb|tvdb|tmdb)-([A-Za-z0-9]+)\]")
# Public: index.py and aliases.py reuse this to read the episode numbers off a
# filename whose series was identified some other way.
SE_RE = re.compile(r"\bS(\d{1,3})E(\d{1,4})\b", re.IGNORECASE)
# The absolute number sits between the SxxExx block and the episode title. Never
# read this directly -- `absolute_from` below applies the plausibility rules that
# stop a resolution or a year being scrobbled as an episode number.
_ABS_RE = re.compile(r"\bS\d{1,3}E\d{1,4}\b\s*-\s*(\d{2,4})\s*-\s", re.IGNORECASE)
# Any NNNp is a frame height: [WEBRip-1080p], [WEBDL-2160p], a bare 720p in a
# scene name. Matched outside the brackets too, because an untagged 1080p is
# just as much a resolution as a tagged one.
_RES_RE = re.compile(r"(\d{3,4})p\b", re.IGNORECASE)
# Above this, a candidate is far likelier a year than an episode. The
# longest-running series anyone numbers absolutely sit near 1,200 (One Piece,
# Detective Conan, Pokemon), so the bound costs nothing real and rejects the
# whole 2000-2099 year window plus 2160 outright.
_ABS_MAX = 1999

_KEY = {"tvdbid": "tvdb", "tvdb": "tvdb",
        "imdbid": "imdb", "imdb": "imdb",
        "tmdbid": "tmdb", "tmdb": "tmdb"}


def _ids(text: str) -> Ids:
    out: Ids = {}
    for label, value in _ID.findall(text):
        if not value:                      # [imdb-] -- present but empty
            continue
        key = _KEY[label.lower()]
        if key in out:
            # First match per id type wins. Sonarr writes the real tag straight
            # after the series title; anything later is episode-title text that
            # happens to look like a tag, and scanning the whole path the first
            # hit is the series folder rather than some nested decoy.
            continue
        out[key] = int(value) if key in ("tvdb", "tmdb") and value.isdigit() else value
    return out


def absolute_from(stem: str) -> int | None:
    """Return the anime absolute number in *stem*, or None if it is not believable.

    HEURISTIC, and it cannot be made exact. The slot between `SxxExx - ` and the
    episode title holds a bare number, and a bare number is also what a
    resolution and a year look like. Two rules narrow it:

    * reject a value that also appears as a frame height in the same name, so
      `- 1080 - Title [WEBRip-1080p]` yields nothing;
    * reject anything outside 1..1999, so a stray `2024` yields nothing.

    What it cannot catch, stated rather than papered over:

    * a *genuine* absolute of 1080 on a file tagged [WEBDL-1080p] is discarded.
      Real for a long-running anime, and accepted: a missing absolute is a gap
      the sink handles, a wrong one is a wrong scrobble.
    * a genuine absolute above 1999 (Sazae-san territory) is discarded.
    * a bare number in the slot that is neither a year nor a resolution -- a part
      number, or an episode whose title really is `42` -- still reads as an
      absolute. Nothing in the filename distinguishes it.

    Not used: "Sonarr pads to three digits, so a two-digit capture is never
    real". True of every one of the 33 absolute numbers in this library, but it
    would also reject a hand-named file, and the padding is a naming-format
    setting rather than a property of the number.
    """
    m = _ABS_RE.search(stem)
    if not m:
        return None
    n = int(m.group(1))
    if not 1 <= n <= _ABS_MAX:
        return None
    if n in {int(r) for r in _RES_RE.findall(stem)}:
        return None
    return n


def ancestors(path: str) -> list[str]:
    """Directory names above *path*, nearest first, with `..` and `.` cancelled out.

    Lexical only. `Path.resolve()` and `os.path.realpath` both touch the disk and
    follow symlinks, and tiers 1 and 2 must stay pure functions -- so `..` is
    cancelled against the name before it, which is what stops
    `/media/Show [tvdbid-111]/../staging/f.mkv` matching the series folder it
    never actually sits in. The cost of staying lexical: if a component is a
    symlink, `link/../X` normalises to a sibling of the link rather than of its
    target. That is the documented trade for never reading the filesystem.
    """
    return list(reversed(Path(os.path.normpath(path)).parent.parts))


def _title(name: str) -> str:
    """Everything before the first bracket tag. Display only, never matched on."""
    return re.split(r"\s*\[", name, maxsplit=1)[0].strip()


def parse(path: str) -> Resolution:
    """Read season/episode/ids straight out of the filename and folder, no I/O."""
    stem = Path(path).stem

    # The filename wins; the folder is a fallback for files that never had a tag.
    ids = _ids(stem) or _ids(path)
    if not ids:
        return UNKNOWN

    se = SE_RE.search(stem)
    if se:
        if "tvdb" not in ids:
            return UNKNOWN                 # episode numbers with no series id say nothing
        return Resolution(
            kind="episode", ids=ids,
            season=int(se.group(1)), episode=int(se.group(2)),
            absolute=absolute_from(stem),
            title=_title(stem), confidence="exact", source="filename")

    if "imdb" in ids or "tmdb" in ids:
        return Resolution(kind="movie", ids=ids, season=None, episode=None,
                          absolute=None, title=_title(stem),
                          confidence="exact", source="filename")

    return UNKNOWN
