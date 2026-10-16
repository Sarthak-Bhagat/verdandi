"""The last sessions in the log, one aligned line each: when, what, episode, how far, why it ended.

**Watched and reached are different words on purpose.** `watched N%` is
`content_seconds / duration`, media actually played through. `reached N%` is
`max_pos / duration`, the furthest playback got, which a jump to the credits
also gets to. Rows written before 2026-10-11 have no `content_seconds`, so for
them `reached` is all there is, and it is labelled as such rather than passed
off as a watch. Either share is floored: 79.9% has not reached 80%.

**A field a row lacks is shown as missing, never guessed.** An old row has no
end reason, so that column is blank; a row with no duration has no share, so
it is `?`. A row whose `ts` cannot be read is still shown, with `?` for its
time: the session happened, and a bad stamp should cost the stamp, not the line.
The same goes for a number that is no number -- `NaN`, `Infinity`, `true` --
which costs the progress column and nothing else.

Times are local. Rows are stamped in UTC, and a session is remembered by the
evening it was watched, not by the hour in Greenwich.
"""

import math
import time
from datetime import datetime
from pathlib import PurePath

from ._store import JsonObject


def _num(row: JsonObject, name: str) -> float | None:
    """Return *name* as a finite float, or None if it is missing or no usable number.

    `json.loads` accepts `NaN`, `Infinity` and ints past any float, and `True`
    is an int to Python; none of them is a measurement.
    """
    v = row.get(name)
    if not isinstance(v, int | float) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _when(row: JsonObject) -> str:
    """Return the row's `ts` as local `YYYY-MM-DD HH:MM`, or `?` if it cannot be read."""
    ts = row.get("ts")
    if not isinstance(ts, str):
        return "?"
    try:
        at = datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


def _title(row: JsonObject) -> str:
    """Return the row's title, or its file's name when it has none.

    An empty title falls back too: an unidentified session is recorded with
    the title `""`, and a blank column says less than the file name does.
    """
    title = row.get("title")
    if isinstance(title, str) and title:
        return title
    path = str(row.get("path") or "")
    return PurePath(path).name or path


def _episode(row: JsonObject) -> str:
    season = row.get("season")
    if season is None:
        return "—"
    episode = row.get("episode")
    return f"S{season}E{'?' if episode is None else episode}"


def _progress(row: JsonObject) -> str:
    """Return the watched share, else the reached share, else `?`.

    A field that is absent or null is skipped, as an old row's `content_seconds`
    is. One that is present but no usable number ends the search at `?`: falling
    through to `reached` would show a row as unmeasured when it was mismeasured.
    """
    duration = _num(row, "duration")
    if not duration:  # None, or a zero no share can be taken of
        return "?"
    for name, word in (("content_seconds", "watched"), ("max_pos", "reached")):
        if row.get(name) is None:
            continue
        v = _num(row, name)
        share = None if v is None else 100 * v / duration
        # Finite inputs can still make an infinite share: 1e300 over 1e-300.
        if share is None or not math.isfinite(share):
            return "?"
        return f"{word} {math.floor(share)}%"
    return "?"


def _end_reason(row: JsonObject) -> str:
    reason = row.get("end_reason")
    return reason if isinstance(reason, str) else ""


def lines(rows: list[JsonObject], n: int) -> list[str]:
    """Return the last *n* of *rows*, oldest first, as lines with aligned columns.

    Each column is padded to its widest value among the rows shown, two spaces
    apart; titles are never cut. Trailing space is dropped, so a row with no
    end reason ends at its progress.
    """
    table = [[_when(r), _title(r), _episode(r), _progress(r), _end_reason(r)] for r in rows[-n:]]
    if not table:
        return []
    widths = [max(len(cells[i]) for cells in table) for i in range(len(table[0]))]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True)).rstrip()
        for cells in table
    ]
