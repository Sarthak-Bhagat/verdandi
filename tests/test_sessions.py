"""The append-only session log and its measured dedupe window."""

import socket
from dataclasses import replace

import pytest

from scrobd import sessions
from scrobd.paths import state_dir
from scrobd.resolution import UNKNOWN, Resolution
from scrobd.session import Session


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def ep(n=1):
    return Resolution(
        kind="episode",
        ids={"tvdb": 1},
        season=1,
        episode=n,
        absolute=None,
        title="Show",
        confidence="exact",
        source="filename",
    )


def sess(path="/m/a.mkv", max_pos=1300.0, duration=1400.0, start=0.0, end=1400.0):
    # A session with no position reported has no first position either.
    return Session(
        session=f"id-{path}-{start}",
        path=path,
        started_at=start,
        ended_at=end,
        duration=duration,
        first_pos=0.0 if max_pos is not None else None,
        max_pos=max_pos,
        samples=10,
        content_seconds=max_pos or 0.0,
        wall_seconds=max_pos or 0.0,
        speed_last=1.0,
        speed_max=1.0,
        end_reason="eof",
    )


def test_a_row_carries_how_it_was_played_and_where():
    """The fields the watched rule and ingest need, written as the accumulator measured them."""
    s = replace(
        sess(),
        content_seconds=1180.5,
        wall_seconds=590.25,
        speed_last=2.0,
        speed_max=2.5,
        end_reason="quit",
    )
    sessions.record(s, ep(), now=1000.0)
    r = sessions.read()[0]
    assert r["session"] == s.session
    assert (r["content_seconds"], r["wall_seconds"]) == (1180.5, 590.25)
    assert (r["speed_last"], r["speed_max"]) == (2.0, 2.5)
    assert r["end_reason"] == "quit"
    assert r["client"] == socket.gethostname()


def test_a_session_is_appended():
    assert sessions.record(sess(), ep(), now=1000.0) is True
    rows = sessions.read()
    assert len(rows) == 1
    assert rows[0]["ids"] == {"tvdb": 1}
    assert rows[0]["season"] == 1
    assert rows[0]["max_pos"] == 1300.0


def test_row_is_exactly_the_line_record_writes():
    """One builder, so a caller logging a session cannot drift from what was written."""
    sessions.record(sess(), ep(), now=1000.0)
    assert sessions.read() == [sessions.row(sess(), ep(), now=1000.0)]


def test_first_pos_is_written_and_a_missing_one_is_null():
    """Where the play began, so a reopen at the resume point reads as one; never a guess."""
    sessions.record(replace(sess(max_pos=1305.0), first_pos=1300.0), ep(1), now=1000.0)
    sessions.record(sess(max_pos=None), ep(2), now=1000.0)
    assert [(r["first_pos"], r["max_pos"]) for r in sessions.read()] == [
        (1300.0, 1305.0),
        (None, None),
    ]


def test_watched_is_not_stored_as_a_flag():
    """It is derived from max_pos at read time, which keeps the log append-only."""
    sessions.record(sess(), ep(), now=1000.0)
    assert "watched" not in sessions.read()[0]


def test_a_partial_watch_is_still_recorded():
    """A 30% abandon is real data. No sink can hold it; this log can."""
    assert sessions.record(sess(max_pos=420.0), ep(), now=1000.0) is True
    assert sessions.read()[0]["max_pos"] == 420.0


def test_the_same_episode_within_the_window_is_not_written_twice():
    sessions.record(sess(), ep(5), now=1000.0)
    assert sessions.record(sess(), ep(5), now=1000.0 + 3600) is False
    assert len(sessions.read()) == 1


def test_the_same_episode_after_the_window_is_a_real_rewatch():
    sessions.record(sess(), ep(5), now=1000.0)
    later = 1000.0 + sessions.DEDUPE_WINDOW_S + 1
    assert sessions.record(sess(), ep(5), now=later) is True
    assert len(sessions.read()) == 2


def test_a_resume_inside_the_window_that_reaches_further_is_written():
    """Stop at 30%, finish two hours later: the window drops duplicate plays, not resumes."""
    sessions.record(sess(max_pos=420.0), ep(5), now=1000.0)
    assert sessions.record(sess(max_pos=1300.0), ep(5), now=1000.0 + 7200) is True
    assert [r["max_pos"] for r in sessions.read()] == [420.0, 1300.0]


def test_a_missing_position_neither_adds_progress_nor_blocks_it():
    sessions.record(sess(max_pos=None), ep(5), now=1000.0)
    assert sessions.record(sess(max_pos=600.0), ep(5), now=1060.0) is True
    assert sessions.record(sess(max_pos=None), ep(5), now=1120.0) is False
    assert [r["max_pos"] for r in sessions.read()] == [None, 600.0]


def test_a_different_episode_within_the_window_is_not_deduped():
    sessions.record(sess(), ep(5), now=1000.0)
    assert sessions.record(sess(), ep(6), now=1000.0 + 60) is True
    assert len(sessions.read()) == 2


def test_an_unidentified_session_is_still_recorded():
    """Nothing is silently discarded -- that is the whole promise."""
    assert sessions.record(sess(path="/m/mystery.mkv"), UNKNOWN, now=1000.0) is True
    row = sessions.read()[0]
    assert row["ids"] == {}
    assert row["confidence"] == "none"
    assert row["path"] == "/m/mystery.mkv"


def test_unidentified_sessions_are_never_deduped_against_each_other():
    """Two unknowns are not the same thing just because both are unknown."""
    sessions.record(sess(path="/m/one.mkv"), UNKNOWN, now=1000.0)
    assert sessions.record(sess(path="/m/two.mkv"), UNKNOWN, now=1000.0 + 60) is True
    assert len(sessions.read()) == 2


def test_a_corrupt_line_does_not_destroy_the_log():
    sessions.record(sess(), ep(), now=1000.0)
    with (state_dir() / "sessions.jsonl").open("a") as fh:
        fh.write("{not json\n")
    sessions.record(sess(), ep(2), now=2000.0)
    assert len(sessions.read()) == 2  # the bad line is skipped, not fatal


def test_a_torn_last_line_costs_only_itself():
    """A write cut off mid-line must not swallow the next row appended after it."""
    sessions.record(sess(), ep(), now=1000.0)
    with (state_dir() / "sessions.jsonl").open("a") as fh:
        fh.write('{"ts": "1970-01-01T00:3')  # no newline: the process died here
    assert sessions.record(sess(), ep(2), now=2000.0) is True
    assert [r["episode"] for r in sessions.read()] == [1, 2]


def test_valid_json_that_is_not_a_row_is_skipped():
    """`[]` parses cleanly and has no `.get` -- the shape bug `_store.read_table` guards."""
    with (state_dir() / "sessions.jsonl").open("a") as fh:
        fh.write("[]\n5\n")
    assert sessions.record(sess(), ep(), now=1000.0) is True
    assert len(sessions.read()) == 1


def test_a_row_carries_its_skips_as_from_to_pairs():
    s = replace(sess(), skips=((60.0, 150.0), (1250.0, 1340.0)))
    sessions.record(s, ep(), now=1000.0)
    assert sessions.read()[0]["skips"] == [[60.0, 150.0], [1250.0, 1340.0]]


def test_a_row_with_no_skips_says_so():
    """An empty list, not a missing field: a reader can tell none from not recorded."""
    sessions.record(sess(), ep(), now=1000.0)
    assert sessions.read()[0]["skips"] == []
