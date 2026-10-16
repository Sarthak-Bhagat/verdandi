"""trakt-scrobbler's record, copied out of its log, and compared with verdandi's own.

For the parallel run: verdandi is validated against the incumbent watching the
same mpv, three rolling months in a row with no difference nobody can explain.
The Trakt API is closed to new free apps since mid-2026, so the incumbent's
record is read from the log it already writes, not from Trakt.

**Copy, because the log rotates.** trakt-scrobbler keeps 128 KiB x 6 files -- a
few weeks at most -- and a monthly comparison needs the whole month. `sync`
runs at the start of every `verdandi watch`, appends whatever is new, and costs
nothing on a day nothing was watched.

**Four shapes, and nothing else, ever.** That log holds the Trakt token, in the
DEBUG request dumps stock trakt-scrobbler writes before every discarded
scrobble, and it is deliberately left stock until it is retired. So no raw line
is stored: a line is copied only if it comes from the `scrobbler` module and its
message is one of the four shapes below, and only the fields parsed out of it are
kept. The one DEBUG line read is the attempt -- `Scrobbling stop at 91.20% for
<title>` -- because it alone names the item a following discard belongs to.

**Watched means different things on each side, on purpose.** trakt-scrobbler
reports *position*, and Trakt marks an episode watched at a `stop` of 80% or
more. verdandi counts *content* -- media actually played. A skip to the credits is
watched on one side and not the other, and `compare` names that as an
explained difference rather than a failure.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from . import _log
from ._store import JsonObject
from .paths import state_dir

_FILE = "trakt-scrobbler.jsonl"

# The first day sessions carried `content_seconds`. Window 1 starts here.
_FIRST_WINDOW = (2026, 10, 11)

# Trakt's rule, and verdandi's §7.3 rule: 80% or more is watched.
_WATCHED = 0.80

# A repeat of the same episode on trakt-scrobbler's side inside this long is
# its own duplicate, the artefact Watch Tracking measured (47% of apparent
# rewatches were one episode logged twice inside a day).
_DUPLICATE_S = 24 * 3600

# How far from a verdandi session a trakt-scrobbler attempt may be and still be
# the same play. An attempt is logged when mpv stops, as the session ends.
_SAME_PLAY_S = 3600

_LINE = re.compile(
    r"(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - "
    r"(?P<level>DEBUG|INFO|WARNING) - .+? - scrobbler - (?P<msg>.*)"
)
_SUCCESS = re.compile(
    r"Scrobble (?P<action>start|pause|resume|stop) successful for (?P<name>.+)"
    r" at (?P<pct>\d+(?:\.\d+)?)%"
)
_ATTEMPT = re.compile(
    r"Scrobbling (?P<action>start|pause|stop) at (?P<pct>\d+(?:\.\d+)?)% "
    r"for (?P<title>.+)"
)
_EPISODE = re.compile(r"(?P<title>.+) S(?P<season>\d+)E(?P<episode>\d+)")
_DISCARDED = "Scrobble unsuccessful. Discarding it."
_DEFERRED = "Scrobble unsuccessful. Will try again later."


class Entry(TypedDict):
    """One thing trakt-scrobbler logged, reduced to the fields `compare` reads."""

    at: str
    t: float
    kind: str  # scrobbled | attempt | discarded | deferred
    action: str | None
    title: str | None
    season: int | None
    episode: int | None
    progress: float | None


def log_dir() -> Path:
    """Return trakt-scrobbler's data directory. Never created: it is not verdandi's."""
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "trakt-scrobbler"


def parse_local(stamp: str) -> float:
    """Return trakt-scrobbler's local-time stamp, `YYYY-MM-DD HH:MM:SS`, as a Unix time."""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").astimezone().timestamp()


def _entry(t: float, kind: str) -> Entry:
    """Return an entry of *kind* at Unix time *t*, every other field empty."""
    at = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Entry(
        at=at, t=t, kind=kind, action=None, title=None, season=None, episode=None, progress=None
    )


def _attempt(t: float, msg: str) -> Entry | None:
    a = _ATTEMPT.fullmatch(msg)
    if a is None:
        return None
    e = _entry(t, "attempt")
    e["action"], e["title"], e["progress"] = a["action"], a["title"], float(a["pct"])
    return e


def _success(t: float, msg: str) -> Entry | None:
    s = _SUCCESS.fullmatch(msg)
    if s is None:
        return None
    e = _entry(t, "scrobbled")
    e["action"], e["title"], e["progress"] = s["action"], s["name"], float(s["pct"])
    if ep := _EPISODE.fullmatch(s["name"]):
        e["title"], e["season"], e["episode"] = ep["title"], int(ep["season"]), int(ep["episode"])
    return e


def _warning(t: float, msg: str) -> Entry | None:
    kind = {_DISCARDED: "discarded", _DEFERRED: "deferred"}.get(msg)
    return None if kind is None else _entry(t, kind)


def parse(line: str) -> Entry | None:
    """Return *line* as an entry if it is one of the four shapes kept, else None."""
    m = _LINE.fullmatch(line.rstrip("\n"))
    if m is None:
        return None
    t, msg = parse_local(m["when"]), m["msg"]
    match m["level"]:
        case "DEBUG":
            return _attempt(t, msg)
        case "INFO":
            return _success(t, msg) or _attempt(t, msg)
        case _:
            return _warning(t, msg)


def _key(e: Entry | JsonObject) -> tuple[object, ...]:
    return tuple(e.get(k) for k in ("at", "kind", "action", "title", "progress"))


def _read_logs() -> list[Entry]:
    """Every kept entry in trakt-scrobbler's current logs, oldest file first."""
    base = log_dir() / "trakt_scrobbler.log"
    rotated = sorted(
        base.parent.glob("trakt_scrobbler.log.*"),
        key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0,
    )
    out: list[Entry] = []
    for p in [*rotated, base]:
        if not p.is_file():
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        out.extend(e for e in map(parse, lines) if e is not None)
    return out


def read() -> list[JsonObject]:
    """Return every entry copied so far, oldest first."""
    p = state_dir() / _FILE
    if not p.exists():
        return []
    rows: list[JsonObject] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def sync() -> int:
    """Append whatever trakt-scrobbler logged that is not copied yet; return how many.

    Never raises. It runs as `watch` starts, and a log it cannot read must not
    cost the watch it is starting.
    """
    try:
        seen = {_key(r) for r in read()}
        new = [e for e in _read_logs() if _key(e) not in seen]
        # Identical lines inside one second collapse to one entry; trakt-scrobbler
        # does not repeat a scrobble that fast, so nothing real is lost.
        unique = list({_key(e): e for e in new}.values())
        if unique:
            with (state_dir() / _FILE).open("a", encoding="utf-8") as f:
                f.writelines(json.dumps(e) + "\n" for e in unique)
    except (OSError, ValueError) as exc:
        _log.event("traktlog_failed", error=str(exc))
        return 0
    return len(unique)


# --- comparing ---


def window(n: int) -> tuple[float, float]:
    """Return the start and end, as Unix times, of rolling window *n* (from 1)."""
    y, m, d = _FIRST_WINDOW

    def month_start(offset: int) -> float:
        yy, mm = divmod(m - 1 + offset, 12)
        return datetime(y + yy, mm + 1, d).astimezone().timestamp()

    return month_start(n - 1), month_start(n)


def current_window(now: float) -> int:
    """Return the window *now* falls in, or 1 before the first starts."""
    n = 1
    while window(n)[1] <= now:
        n += 1
    return n


def _norm(title: object) -> str:
    """Return a title reduced to what both sides spell alike: no year, case or punctuation."""
    text = re.sub(r"\s*\(\d{4}\)\s*$", "", str(title or "")).casefold()
    return re.sub(r"[^0-9a-z]+", " ", text).strip()


@dataclass
class Difference:
    """One watch only one side recorded."""

    side: str  # "only verdandi" | "only trakt-scrobbler"
    title: str
    season: int | None
    episode: int | None
    at: float
    reason: str | None = None


@dataclass
class Comparison:
    """One window, lined up."""

    window: int
    matched: list[tuple[JsonObject, JsonObject]] = field(default_factory=list)
    explained: list[Difference] = field(default_factory=list)
    unexplained: list[Difference] = field(default_factory=list)


def _num(row: JsonObject, name: str) -> float | None:
    v = row.get(name)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _share(row: JsonObject, name: str) -> float | None:
    """Return *name* as a share of the row's duration, or None if either is missing."""
    v, duration = _num(row, name), _num(row, "duration")
    return v / duration if v is not None and duration else None


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _ident(row: JsonObject) -> tuple[str, int | None, int | None]:
    return _norm(row.get("title")), _int(row.get("season")), _int(row.get("episode"))


def _diff(side: str, row: JsonObject, at: float, reason: str | None = None) -> Difference:
    return Difference(
        side=side,
        title=str(row.get("title") or ""),
        season=_int(row.get("season")),
        episode=_int(row.get("episode")),
        at=at,
        reason=reason,
    )


def _trakt_reason(
    row: JsonObject,
    at: float,
    verdandi_skips: list[JsonObject],
    matched: list[tuple[JsonObject, JsonObject]],
) -> str | None:
    """Why a watch only trakt-scrobbler recorded is not a fault, if it is not."""
    ident = _ident(row)
    if any(_ident(t) == ident and 0 <= at - (_num(t, "t") or 0) < _DUPLICATE_S for t, _ in matched):
        return "trakt-scrobbler logged it twice inside a day"
    for s in verdandi_skips:
        if _ident(s) == ident:
            verdandi_skips.remove(s)
            return "skipped: reached the end without watching it"
    return None


def _verdandi_reason(row: JsonObject, mirror: list[JsonObject]) -> str | None:
    """Why a watch only verdandi recorded is not a fault, if it is not."""
    ended = _num(row, "ended_at") or 0.0
    for i, e in enumerate(mirror[:-1]):
        if (
            e.get("kind") == "attempt"
            and _norm(e.get("title")) == _norm(row.get("title"))
            and abs((_num(e, "t") or 0.0) - ended) <= _SAME_PLAY_S
        ):
            follow = mirror[i + 1].get("kind")
            if follow == "discarded":
                return "trakt-scrobbler discarded it"
            if follow == "deferred":
                return "trakt-scrobbler is still retrying it"
    return None


def compare(n: int, rows: list[JsonObject], mirror: list[JsonObject]) -> Comparison:
    """Line up window *n* of verdandi's *rows* against trakt-scrobbler's *mirror*."""
    start, end = window(n)
    out = Comparison(window=n)

    def inside(t: float | None) -> bool:
        return t is not None and start <= t < end

    # Only rows that can say whether they were watched: those carrying content.
    measured = [
        r for r in rows if inside(_num(r, "ended_at")) and _share(r, "content_seconds") is not None
    ]
    watched = [r for r in measured if (_share(r, "content_seconds") or 0.0) >= _WATCHED]
    skips = [
        r
        for r in measured
        if (_share(r, "content_seconds") or 0.0) < _WATCHED
        and (_share(r, "max_pos") or 0.0) >= _WATCHED
    ]
    trakt = sorted(
        (
            e
            for e in mirror
            if e.get("kind") == "scrobbled"
            and e.get("action") == "stop"
            and (_num(e, "progress") or 0.0) >= _WATCHED * 100
            and inside(_num(e, "t"))
        ),
        key=lambda e: _num(e, "t") or 0.0,
    )
    for t in trakt:
        at = _num(t, "t") or 0.0
        hit = next((r for r in watched if _ident(r) == _ident(t)), None)
        if hit is not None:
            watched.remove(hit)
            out.matched.append((t, hit))
            continue
        reason = _trakt_reason(t, at, skips, out.matched)
        (out.explained if reason else out.unexplained).append(
            _diff("only trakt-scrobbler", t, at, reason)
        )
    window_mirror = [e for e in mirror if inside(_num(e, "t"))]
    for r in watched:
        at = _num(r, "ended_at") or 0.0
        reason = _verdandi_reason(r, window_mirror)
        (out.explained if reason else out.unexplained).append(_diff("only verdandi", r, at, reason))
    return out
