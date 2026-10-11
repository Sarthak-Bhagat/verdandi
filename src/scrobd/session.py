"""One record per file played, built from a stream of mpv property changes and events.

Sessions, not thresholds. The record carries start, end, the position the play
began at and the furthest it reached, and how much of the file was actually
played; whether that counts as "watched" is derived at read time from
`content_seconds / duration` and never stored. A 30% abandon is real data -- no
sink can hold it, this log can.

**Content, not position.** `max_pos` is how far playback *reached*, and a jump to
the credits reaches the end. `content_seconds` is media actually played through:
position advanced between two reports, unpaused, with no seek in between. Speed
does not change it -- an episode watched whole at 2x is a whole episode -- and
`wall_seconds`, the real time spent unpaused, is the number speed does change.

Pure: no socket, no clock, no randomness. `now` is supplied by the caller, and
so is the source of session ids, so tests drive both directly.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

# A position report may run this far ahead of what the clock and the speed
# allow before the advance counts as a skip rather than watching. mpv reports
# position several times a second, so an honest advance overshoots by a frame
# or two; a second of slack covers that and is still far below any real skip.
_SEEK_SLACK_S = 1.0


@dataclass(frozen=True)
class Session:
    """One continuous play of one file.

    `first_pos` is the first position mpv reported -- where this play began.
    With save-position-on-quit, a five-second reopen at the resume point
    reaches a `max_pos` that on its own reads like a whole watch; beside
    `first_pos` it reads as five seconds. Rows cannot be back-filled once
    written, so it is recorded from the start.

    `end_reason` is mpv's own -- `eof`, `stop`, `quit`, `error`, `redirect` --
    when it sent one, and otherwise the caller's: `signal` when the watcher was
    told to stop, `lost` when mpv went away without a word. None means the
    session was closed by the next file with no `end-file` before it.
    """

    session: str
    path: str
    started_at: float
    ended_at: float
    duration: float | None
    first_pos: float | None
    max_pos: float | None
    samples: int
    content_seconds: float
    wall_seconds: float
    speed_last: float | None
    speed_max: float | None
    end_reason: str | None


def _random_id() -> str:
    return uuid.uuid4().hex


class Accumulator:
    """Folds property changes and mpv's `seek` and `end-file` events into one Session per file."""

    def __init__(self, new_id: Callable[[], str] = _random_id) -> None:
        """Start with nothing playing and nothing waiting to be taken.

        *new_id* names each session as it opens. The log keys on it, so a
        session sent twice can be recognised as one.
        """
        self._new_id = new_id
        self._path: str | None = None
        self._id = ""
        self._done: Session | None = None
        # Player state, not file state: mpv keeps both across files and reports
        # each once, when the watcher subscribes, and again only on a change.
        self._paused = False
        self._speed: float | None = None
        self._reset(0.0)

    def feed(self, name: str, data: object, now: float) -> None:
        """Absorb one property change, or a `seek` or `end-file` event.

        Only `path` can end a session. Everything else -- `pause` above all --
        is absorbed into the open one: pausing for an hour mid-film is the most
        ordinary thing that happens, and must not split one watch into two.
        """
        if name == "path":
            # mpv reports `path` as null when it goes idle between files. That
            # is not a new file, so it does not open one; the session stays
            # open until a real path arrives or the caller calls `finish`.
            if isinstance(data, str) and data != self._path:
                self._close(now)
                self._open(data, now)
            return
        if name == "pause" and isinstance(data, bool):
            self._tick(now)
            self._paused = data
            self._ref = None        # position resumes from wherever playback restarts
            return
        if name == "speed" and isinstance(data, int | float):
            self._set_speed(float(data))
            return
        if self._path is not None:
            self._last = now
            self._file_change(name, data, now)

    def _file_change(self, name: str, data: object, now: float) -> None:
        """Absorb a change that belongs to the open file rather than to the player."""
        if name == "duration" and isinstance(data, int | float):
            self._duration = float(data)
        elif name == "time-pos" and isinstance(data, int | float):
            self._position(float(data), now)
        elif name == "seek":
            self._seeked = True
        elif name == "end-file":
            # Sent before the path changes, so this is still the file it names.
            self._tick(now)
            self._ended = True
            if isinstance(data, str) and self._end_reason is None:
                self._end_reason = data

    def finish(self, now: float, reason: str | None = None) -> None:
        """End the current session, if any, giving *reason* only if mpv gave none."""
        if self._path is not None and self._end_reason is None:
            self._end_reason = reason
        self._close(now)

    def take(self) -> Session | None:
        """Return the most recently finished session, once.

        One slot, not a queue: the caller is expected to take after every
        `feed` and `finish`, which is the only way at most one session can
        close between takes. A caller that batches instead would lose the
        earlier of two.
        """
        done, self._done = self._done, None
        return done

    def _position(self, pos: float, now: float) -> None:
        """Absorb one position report: progress, and any content played since the last."""
        self._samples += 1
        if self._first_pos is None:
            self._first_pos = pos
        # Monotonic on purpose. Watching to 90% and rewinding to rewatch a
        # scene is still a 90% watch, so a later, lower position never
        # overwrites a higher earlier one.
        if self._max_pos is None or pos > self._max_pos:
            self._max_pos = pos
        self._tick(now)
        if self._paused or self._ended:
            self._ref = None
            return
        if self._ref is not None and not self._seeked:
            ref_pos, ref_now = self._ref
            advance = pos - ref_pos
            # Backwards is a seek mpv did not announce; further than the clock
            # and the speed allow is one too. Either way, not watching.
            if 0.0 <= advance <= (now - ref_now) * (self._speed or 1.0) + _SEEK_SLACK_S:
                self._content += advance
        self._ref = (pos, now)
        self._seeked = False

    def _tick(self, now: float) -> None:
        """Add the real time since the last tick to `wall_seconds`, if it was spent playing."""
        if self._path is not None and not self._paused and not self._ended:
            self._wall += max(0.0, now - self._wall_ref)
        self._wall_ref = now

    def _set_speed(self, speed: float) -> None:
        self._speed = speed
        if self._path is not None:
            self._speed_last = speed
            self._speed_max = speed if self._speed_max is None else max(self._speed_max, speed)

    def _reset(self, now: float) -> None:
        """Clear everything that belongs to one file."""
        self._started = self._last = self._wall_ref = now
        self._duration: float | None = None
        self._first_pos: float | None = None
        self._max_pos: float | None = None
        self._samples = 0
        self._content = 0.0
        self._wall = 0.0
        self._ref: tuple[float, float] | None = None
        self._seeked = False
        self._ended = False
        self._end_reason: str | None = None
        self._speed_last: float | None = self._speed
        self._speed_max: float | None = self._speed

    def _open(self, path: str, now: float) -> None:
        """Begin a session for *path*, discarding any leftover state."""
        self._reset(now)
        self._path = path
        self._id = self._new_id()

    def _close(self, now: float) -> None:
        """Freeze the open session, if there is one, into the take slot."""
        if self._path is None:
            return
        self._tick(now)
        # `duration`, `first_pos` and `max_pos` stay None when mpv never
        # reported them. A missing number is a gap the reader can handle; a
        # guessed one is a lie in a watch history. Nothing here derives "watched".
        self._done = Session(session=self._id, path=self._path, started_at=self._started,
                             ended_at=max(now, self._last), duration=self._duration,
                             first_pos=self._first_pos, max_pos=self._max_pos,
                             samples=self._samples, content_seconds=round(self._content, 3),
                             wall_seconds=round(self._wall, 3), speed_last=self._speed_last,
                             speed_max=self._speed_max, end_reason=self._end_reason)
        self._path = None
