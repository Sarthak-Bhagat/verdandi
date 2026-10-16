"""The local record of every session played, one JSON object per line.

**Append-only.** A row is written once and never rewritten, which is why there
is no `watched` field: whether a session counts as watched is derived at read
time from `content_seconds / duration`, so changing the threshold later
reinterprets history instead of requiring it to be edited. Not from `max_pos`:
that is how far playback reached, and a jump to the credits reaches the end. A
30% abandon is a row like any other. No sink can hold it; this log can.
`duration`, `first_pos` and `max_pos` are written as `null` when mpv never
reported them -- a missing number is a gap, a guessed one is a lie.

Rows written before 2026-10-11 lack `session`, `client`, `end_reason` and the
time and speed fields; a reader treats each as missing, never as zero. Rows from
before skip recording lack `skips`, which is not the same as `[]`: an empty list
means no forward jump happened, a missing field means none were looked for.

**Nothing is discarded for being unidentified.** A session the resolver could
not name is still a row, with empty `ids` and confidence `"none"`, so the watch
survives until the question is answered.

**The dedupe window is measured, not guessed.** Over 5,685 organic plays, the
genuine rewatch rate is 2.22%, and 47% of apparent rewatches are the same
episode logged twice inside a day. A repeat of the same identity within 24
hours is therefore far more likely an artifact than a real second watch, and it
does not become a second row. Past the window, a repeat is a rewatch and is kept.

But that measurement counted *plays* -- completed scrobbles -- and this log
records *sessions*, several of which can make up one play. Stop an episode at
30% and finish it two hours later, and the second session is the same identity
inside the window; dropping it would record a completed watch as an abandon.
So the window removes duplicate plays, not resumes: a repeat inside it is
dropped only if it adds no progress -- its `max_pos` reaches no further than
the furthest already logged for that identity in the window. A session that
reaches further is written. Collapsing several rows of one play into a single
scrobble is the sink's job, later; a few extra rows cost nothing here.

The identity compared is the full one: `ids`, `season` and `episode` together.
A session with **no ids never dedupes**. Two unknowns are not the same thing
just because both are unknown, and folding them would silently drop a watch.

**A corrupt line costs one row, not the log** -- the same promise
`_store.read_table` makes for whole-table files. `read` skips any line that is
not a JSON object, and `record` starts a fresh line if the last write was cut
off mid-line, so one torn write cannot take the next row down with it.

No clock, like `session`: `now` is supplied by the caller, so tests drive the
window directly instead of sleeping.
"""

import json
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from ._store import JsonObject
from .paths import state_dir
from .resolution import Confidence, Ids, Kind, Resolution, Source
from .session import Session

# 24 hours. Measured over 5,685 organic plays: genuine rewatches are 2.22% of
# them, and 47% of apparent rewatches are one episode logged twice inside a day.
DEDUPE_WINDOW_S = 24 * 3600

# The machine that played it. One today; the field is there for the second.
_CLIENT = socket.gethostname()

# Whole seconds and a `Z`, the spelling `_log` uses and `jq`'s `fromdateiso8601`
# parses. Dropping the fraction moves a row's time by under a second, which a
# 24-hour window cannot notice, and the file's own line order keeps sequence.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Row(TypedDict):
    """One line of the log, as `row` builds it. Rows read back are not assumed to be one."""

    ts: str
    session: str
    client: str
    path: str
    started_at: float
    ended_at: float
    end_reason: str | None
    duration: float | None
    first_pos: float | None
    max_pos: float | None
    content_seconds: float
    wall_seconds: float
    speed_last: float | None
    speed_max: float | None
    samples: int
    skips: list[list[float]]
    kind: Kind
    ids: Ids
    season: int | None
    episode: int | None
    absolute: int | None
    title: str
    confidence: Confidence
    source: Source


def _file() -> Path:
    return state_dir() / "sessions.jsonl"


def log_path() -> Path:
    """Return the session log's path. Public for `push`, which reads it from a cursor."""
    return _file()


def _raw() -> bytes:
    """Return the log's bytes, or nothing if no session has been recorded yet."""
    try:
        return _file().read_bytes()
    except FileNotFoundError:
        return b""


def _rows(raw: bytes) -> list[JsonObject]:
    """Parse *raw* into rows, skipping every line that is not a JSON object.

    Bytes, not text: one undecodable byte would otherwise fail the whole read,
    where here it fails `json.loads` for its own line only. Valid JSON of the
    wrong shape is skipped too -- `[]` parses cleanly and then has no `.get`.
    """
    rows: list[JsonObject] = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:  # JSONDecodeError and UnicodeDecodeError both
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read() -> list[JsonObject]:
    """Return every recorded session, oldest first."""
    return _rows(_raw())


def _recorded_at(row: Mapping[str, object]) -> float | None:
    """Return when *row* was recorded, or None if its `ts` cannot be read."""
    ts = row.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _same_identity_in_window(row: Mapping[str, object], resolution: Resolution, now: float) -> bool:
    """Whether *row* is the same identity as *resolution*, recorded inside the window."""
    when = _recorded_at(row)
    # Either side of `now`: a row stamped ahead of a clock that has since been
    # set back is still within a day of this one, and one stamped far ahead
    # must not suppress that identity until the clock catches up.
    return (
        when is not None
        and abs(now - when) < DEDUPE_WINDOW_S
        and row.get("ids") == resolution.ids
        and row.get("season") == resolution.season
        and row.get("episode") == resolution.episode
    )


def _is_duplicate(
    session: Session, resolution: Resolution, now: float, rows: list[JsonObject]
) -> bool:
    """Whether *session* repeats a play already logged inside the window and adds no progress.

    A session with no position adds none. A logged row with no position sets no
    bar, so it never blocks a session that has one.
    """
    if not resolution.ids:
        return False  # two unknowns are not the same thing
    prior = [row for row in rows if _same_identity_in_window(row, resolution, now)]
    if not prior:
        return False
    if session.max_pos is None:
        return True
    reached = [pos for row in prior if isinstance(pos := row.get("max_pos"), int | float)]
    return bool(reached) and session.max_pos <= max(reached)


def row(session: Session, resolution: Resolution, now: float) -> Row:
    """Return the row `record` writes for *session* as *resolution*, recorded at *now*.

    Public so that a caller reporting a session -- to the post-mortem log, or
    to stderr when the write itself failed -- shows exactly what was written,
    or would have been, without keeping a second copy of the field list.
    """
    return {
        "ts": datetime.fromtimestamp(now, UTC).strftime(_TS_FORMAT),
        "session": session.session,
        "client": _CLIENT,
        "path": session.path,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "end_reason": session.end_reason,
        "duration": session.duration,
        "first_pos": session.first_pos,
        "max_pos": session.max_pos,
        "content_seconds": session.content_seconds,
        "wall_seconds": session.wall_seconds,
        "speed_last": session.speed_last,
        "speed_max": session.speed_max,
        "samples": session.samples,
        "skips": [[a, b] for a, b in session.skips],
        "kind": resolution.kind,
        "ids": resolution.ids,
        "season": resolution.season,
        "episode": resolution.episode,
        "absolute": resolution.absolute,
        "title": resolution.title,
        "confidence": resolution.confidence,
        "source": resolution.source,
    }


def record(session: Session, resolution: Resolution, now: float) -> bool:
    """Append *session* as *resolution*; return False if it was a duplicate and not written."""
    raw = _raw()
    if _is_duplicate(session, resolution, now, _rows(raw)):
        return False
    line = json.dumps(row(session, resolution, now)) + "\n"
    if raw and not raw.endswith(b"\n"):
        line = "\n" + line  # end the torn line, so it costs only itself
    with _file().open("a", encoding="utf-8") as fh:
        fh.write(line)
    return True
