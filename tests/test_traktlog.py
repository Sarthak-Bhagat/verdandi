"""Copying trakt-scrobbler's scrobbles out of its rotating log, and comparing them with scrobd's."""

import json
import time
from pathlib import Path

import pytest

from scrobd import sessions, traktlog
from scrobd.paths import state_dir


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("SCROBD_LOG", "0")
    # trakt-scrobbler stamps local time; pin it, so the conversion is the same everywhere.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    time.tzset()


def logdir() -> Path:
    d = Path(traktlog.log_dir())
    d.mkdir(parents=True, exist_ok=True)
    return d


def line(when: str, level: str, msg: str, module: str = "scrobbler") -> str:
    return f"{when},123 - {level} - Thread-1 - {module} - {msg}\n"


def stop(when: str, name: str, pct: float) -> str:
    return line(when, "INFO", f"Scrobble stop successful for {name} at {pct:.2f}%")


def write_log(*lines: str, name: str = "trakt_scrobbler.log") -> None:
    (logdir() / name).write_text("".join(lines))


def mirrored() -> list[dict]:
    p = state_dir() / "trakt-scrobbler.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# --- reading the log ---


def test_a_successful_episode_scrobble_is_read_with_its_numbers():
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5))
    assert traktlog.sync() == 1
    (e,) = mirrored()
    assert (e["kind"], e["action"], e["title"], e["season"], e["episode"], e["progress"]) == (
        "scrobbled", "stop", "Some Show", 2, 5, 93.5)
    assert e["at"] == "2026-10-12T21:00:00Z"


def test_a_film_has_no_episode_numbers():
    write_log(stop("2026-10-12 21:00:00", "Some Film", 88.0))
    traktlog.sync()
    (e,) = mirrored()
    assert (e["title"], e["season"], e["episode"]) == ("Some Film", None, None)


def test_attempts_discards_and_retries_are_kept():
    """A discard names no title; the attempt logged just before it does."""
    write_log(
        line("2026-10-12 21:00:00", "INFO", "Scrobbling stop at 91.20% for Other Show"),
        line("2026-10-12 21:00:01", "WARNING", "Scrobble unsuccessful. Discarding it."),
        line("2026-10-12 21:05:00", "WARNING", "Scrobble unsuccessful. Will try again later."))
    traktlog.sync()
    assert [(e["kind"], e.get("title"), e.get("progress")) for e in mirrored()] == [
        ("attempt", "Other Show", 91.2), ("discarded", None, None), ("deferred", None, None)]


def test_the_stock_debug_attempt_line_is_read():
    """Stock trakt-scrobbler logs the attempt at DEBUG; that one shape is read from DEBUG."""
    write_log(line("2026-10-12 21:00:00", "DEBUG", "Scrobbling stop at 91.20% for Other Show"))
    traktlog.sync()
    assert [(e["kind"], e["title"]) for e in mirrored()] == [("attempt", "Other Show")]


def test_no_other_debug_line_is_read_even_in_a_known_shape():
    write_log(line("2026-10-12 21:00:00", "DEBUG", "Scrobble stop successful for X at 90.00%"),
              line("2026-10-12 21:00:01", "DEBUG", "Scrobble unsuccessful. Discarding it."))
    assert traktlog.sync() == 0


def test_nothing_else_is_ever_copied():
    """A request dump carries the auth header. Only the four known shapes are read."""
    write_log(
        line("2026-10-12 21:00:00", "DEBUG", "Request headers: {'Authorization': 'Bearer abc'}",
             module="trakt_interface"),
        line("2026-10-12 21:00:00", "INFO", "Scrobble stop successful for X at 90.00%",
             module="trakt_interface"),
        line("2026-10-12 21:00:00", "INFO", "Something else entirely"),
        "a line in no format at all\n")
    assert traktlog.sync() == 0
    assert mirrored() == []


def test_a_second_sync_copies_only_what_is_new():
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5))
    traktlog.sync()
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5),
              stop("2026-10-13 21:00:00", "Some Show S02E06", 95.0))
    assert traktlog.sync() == 1
    assert [e["episode"] for e in mirrored()] == [5, 6]


def test_rotated_files_are_read_oldest_first():
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5), name="trakt_scrobbler.log.1")
    write_log(stop("2026-10-13 21:00:00", "Some Show S02E06", 95.0))
    traktlog.sync()
    assert [e["episode"] for e in mirrored()] == [5, 6]


def test_no_trakt_scrobbler_log_is_not_an_error():
    assert traktlog.sync() == 0


# --- comparing ---


def scrobd_row(ended: str, title: str, se: tuple[int, int] | None, *, content: float,
               max_pos: float | None = None) -> None:
    season, episode = se or (None, None)
    end, duration = traktlog.parse_local(ended), 1400.0
    row = {"ts": ended.replace(" ", "T") + "Z", "path": f"/m/{title}.mkv", "started_at": end - 60,
           "ended_at": end, "duration": duration, "first_pos": 0.0,
           "max_pos": duration if max_pos is None else max_pos, "content_seconds": content,
           "kind": "episode" if season else "movie", "ids": {"tvdb": 1}, "season": season,
           "episode": episode, "title": title}
    with (state_dir() / "sessions.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")


def compare(n: int = 1) -> traktlog.Comparison:
    traktlog.sync()
    return traktlog.compare(n, sessions.read(), mirrored())


def test_the_first_window_starts_on_the_day_content_was_first_recorded():
    start, end = traktlog.window(1)
    assert (time.strftime("%F", time.localtime(start)), time.strftime("%F", time.localtime(end))) \
        == ("2026-10-11", "2026-11-11")
    assert time.strftime("%F", time.localtime(traktlog.window(3)[1])) == "2027-01-11"


def test_a_watch_both_recorded_matches():
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5))
    scrobd_row("2026-10-12 21:00:05", "Some Show (2017)", (2, 5), content=1300.0)
    c = compare()
    assert (len(c.matched), c.unexplained) == (1, [])


def test_a_skip_to_the_end_is_explained_not_a_failure():
    """trakt-scrobbler counts position; scrobd counts watching. The skip is the difference."""
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 99.0))
    scrobd_row("2026-10-12 21:00:05", "Some Show", (2, 5), content=200.0)
    c = compare()
    assert c.unexplained == []
    assert [x.reason for x in c.explained] == ["skipped: reached the end without watching it"]


def test_a_discarded_scrobble_explains_a_watch_only_scrobd_has():
    write_log(
        line("2026-10-12 21:00:00", "INFO", "Scrobbling stop at 91.20% for Other Show"),
        line("2026-10-12 21:00:01", "WARNING", "Scrobble unsuccessful. Discarding it."))
    scrobd_row("2026-10-12 21:00:05", "Other Show (2019)", (8, 9), content=1300.0)
    c = compare()
    assert c.unexplained == []
    assert [x.reason for x in c.explained] == ["trakt-scrobbler discarded it"]


def test_a_repeat_inside_a_day_on_trakts_side_is_explained():
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5),
              stop("2026-10-12 22:00:00", "Some Show S02E05", 95.0))
    scrobd_row("2026-10-12 21:00:05", "Some Show", (2, 5), content=1300.0)
    c = compare()
    assert c.unexplained == []
    assert [x.reason for x in c.explained] == ["trakt-scrobbler logged it twice inside a day"]


def test_a_watch_only_one_side_has_and_nothing_explains_is_unexplained():
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5))
    scrobd_row("2026-10-13 21:00:05", "Third Show", (1, 1), content=1300.0)
    c = compare()
    assert sorted((x.side, x.title) for x in c.unexplained) == [
        ("only scrobd", "Third Show"), ("only trakt-scrobbler", "Some Show")]


def test_rows_from_before_content_was_recorded_are_not_compared():
    """Rows without content_seconds cannot say whether a file was watched, so they sit out."""
    write_log(stop("2026-10-12 21:00:00", "Some Show S02E05", 93.5))
    scrobd_row("2026-10-12 21:00:05", "Some Show", (2, 5), content=1300.0)
    with (state_dir() / "sessions.jsonl").open("a") as f:
        f.write(json.dumps({"ts": "2026-10-12T21:30:00Z", "ended_at": 1, "title": "Old"}) + "\n")
    assert compare().unexplained == []


def test_watches_outside_the_window_are_left_out():
    write_log(stop("2026-11-12 21:00:00", "Some Show S02E05", 93.5))
    scrobd_row("2026-11-12 21:00:05", "Third Show", (1, 1), content=1300.0)
    assert compare(1).unexplained == []
    assert len(compare(2).unexplained) == 2
