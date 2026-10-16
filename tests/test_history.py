"""`verdandi history`: the last sessions in the log, one aligned line each."""

import json
import time

import pytest

from verdandi import cli, history
from verdandi.paths import state_dir


@pytest.fixture(autouse=True)
def dublin(monkeypatch):
    """Pin local time to Dublin, so every expected time is the same on any machine.

    `tzset` is called again after the variable is restored: the C library keeps
    the zone it last read, and every later test would otherwise run in Dublin.
    """
    monkeypatch.setenv("TZ", "Europe/Dublin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def row(**fields):
    """Return an old-style row: what every row has, before `content_seconds` and `end_reason`."""
    base = {
        "ts": "2026-01-10T20:00:00Z",  # winter: Dublin is on UTC
        "path": "/media/TV/Some Show/Season 01/Some Show - S01E02.mkv",
        "title": "Some Show",
        "season": 1,
        "episode": 2,
        "duration": 1400.0,
        "max_pos": 1330.0,
        "started_at": 1768075200.0,
        "ended_at": 1768076600.0,
    }
    return base | fields


def only(rows):
    shown = history.lines(rows, 20)
    assert len(shown) == 1, shown
    return shown[0]


def test_a_measured_row_says_how_much_was_watched():
    line = only([row(content_seconds=1120.0, end_reason="eof")])
    assert "watched 80%" in line
    assert "reached" not in line


def test_an_old_row_says_only_how_far_playback_reached():
    line = only([row()])
    assert "reached 95%" in line
    assert "watched" not in line


def test_content_seconds_of_zero_is_watched_not_reached():
    """Zero is a measurement -- nothing was played -- not a missing field."""
    assert "watched 0%" in only([row(content_seconds=0.0)])


def test_without_a_duration_the_progress_is_unknown():
    assert only([row(duration=None)]).split("  ")[-1] == "?"
    assert only([row(max_pos=None)]).split("  ")[-1] == "?"


def test_a_film_has_no_episode():
    line = only([row(title="Some Film", season=None, episode=None)])
    assert "Some Film  —  reached" in line


def test_an_episode_is_unpadded():
    assert "Some Show  S3E14  " in only([row(season=3, episode=14)])


def test_season_zero_is_a_season_not_a_film():
    """Specials are season 0, and jq's `if .season` counts 0 as set."""
    assert "Some Show  S0E3  " in only([row(season=0, episode=3)])


def test_a_season_with_no_episode_marks_the_episode_unknown():
    assert "Some Show  S1E?  " in only([row(episode=None)])


def test_a_row_with_no_title_is_named_by_its_file():
    assert "Some Show - S01E02.mkv  S1E2" in only([row(title=None)])
    no_title = row()
    del no_title["title"]
    assert "Some Show - S01E02.mkv  S1E2" in only([no_title])


def test_an_unidentified_row_is_named_by_its_file_not_left_blank():
    """An unidentified session is recorded with the title `""`, not null."""
    assert "Some Show - S01E02.mkv  S1E2" in only([row(title="")])


def test_only_the_last_n_rows_are_shown_oldest_first():
    rows = [row(title=f"Show {i}") for i in range(5)]
    shown = history.lines(rows, 3)
    assert [line.split("  ")[1] for line in shown] == ["Show 2", "Show 3", "Show 4"]


def test_fewer_rows_than_n_shows_them_all():
    assert len(history.lines([row(), row()], 20)) == 2


def test_no_rows_is_no_lines():
    assert history.lines([], 20) == []


def test_the_time_is_local_not_utc():
    """19:36 UTC in July is 20:36 in Dublin, on summer time."""
    assert only([row(ts="2026-07-14T19:36:12Z")]).startswith("2026-07-14 20:36  ")


def test_the_time_in_winter_is_unshifted():
    assert only([row(ts="2026-01-14T19:36:12Z")]).startswith("2026-01-14 19:36  ")


def test_a_row_whose_time_cannot_be_read_is_still_shown():
    """The session happened; a bad stamp costs its time, not the line."""
    assert only([row(ts="not a time")]).startswith("?  ")
    no_ts = row()
    del no_ts["ts"]
    assert only([no_ts]).startswith("?  ")


def test_the_end_reason_is_shown_when_recorded():
    assert only([row(content_seconds=1120.0, end_reason="quit")]).endswith("watched 80%  quit")


def test_an_old_row_has_no_end_reason_and_no_trailing_space():
    assert only([row()]).endswith("reached 95%")


def test_columns_are_padded_to_the_widest_value_shown():
    rows = [
        row(title="A", content_seconds=1400.0, end_reason="eof"),
        row(title="A Much Longer Title", season=12, episode=10),
        row(title="Some Film", season=None, episode=None, duration=None),
    ]
    assert history.lines(rows, 20) == [
        "2026-01-10 20:00  A                    S1E2    watched 100%  eof",
        "2026-01-10 20:00  A Much Longer Title  S12E10  reached 95%",
        "2026-01-10 20:00  Some Film            —       ?",
    ]


def test_widths_come_from_the_rows_shown_only():
    rows = [row(title="A Title Far Longer Than The Rest"), row(title="Short")]
    assert history.lines(rows, 1) == ["2026-01-10 20:00  Short  S1E2  reached 95%"]


def test_a_long_title_is_not_truncated():
    title = "Some Show With A Title Long Enough To Be Cut Off By A Careless Formatter"
    assert title in only([row(title=title)])


def test_the_share_is_floored_not_rounded():
    assert "watched 79%" in only([row(duration=1000.0, content_seconds=799.0)])
    assert "reached 79%" in only([row(duration=1000.0, max_pos=799.0)])


def test_a_duration_of_zero_has_no_share():
    assert only([row(duration=0.0, max_pos=5.0)]).endswith("  ?")


def test_a_bool_is_not_a_number():
    """`True` is an int to Python. Counted as one it would read as 1 second watched."""
    assert only([row(content_seconds=True)]).endswith("  ?")
    assert only([row(duration=True)]).endswith("  ?")


@pytest.mark.parametrize(
    "fields",
    [
        {"duration": float("nan"), "content_seconds": 5.0},
        {"content_seconds": float("nan")},
        {"content_seconds": float("inf")},
        {"duration": float("inf")},
        {"max_pos": 10**400},  # json.loads reads it as an int; float() of it overflows
        {"duration": 1e-300, "max_pos": 1e300},  # each finite, the share is not
    ],
    ids=["nan-duration", "nan-content", "inf-content", "inf-duration", "huge-int", "huge-share"],
)
def test_a_number_that_is_not_finite_costs_the_progress_not_the_log(fields):
    """`json.loads` accepts NaN, Infinity and ints past any float; none may stop the listing."""
    shown = history.lines([row(**fields), row(title="Other Show")], 20)
    assert shown[0].endswith("  ?")
    assert "Other Show" in shown[1]


def write_log(*lines):
    (state_dir() / "sessions.jsonl").write_text("".join(f"{line}\n" for line in lines))


def test_the_command_prints_the_rows_and_skips_a_line_it_cannot_read(capsys):
    write_log(
        json.dumps(row(title="Some Show")),
        '{"ts": "2026-01-11T',  # torn mid-write
        json.dumps(row(title="Other Show", content_seconds=700.0, end_reason="stop")),
    )
    assert cli.main(["history", "-n", "5"]) == 0
    assert capsys.readouterr().out == (
        "2026-01-10 20:00  Some Show   S1E2  reached 95%\n"
        "2026-01-10 20:00  Other Show  S1E2  watched 50%  stop\n"
    )


def test_the_command_shows_as_many_as_n_asks_for(capsys):
    write_log(*(json.dumps(row(title=f"Show {i}")) for i in range(4)))
    assert cli.main(["history", "-n", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split("  ")[1] for line in out] == ["Show 2", "Show 3"]


def test_the_command_survives_a_non_finite_number_in_the_log(capsys):
    write_log(
        '{"ts": "2026-01-10T20:00:00Z", "title": "Some Show", "duration": NaN, "max_pos": 5}',
        json.dumps(row(title="Other Show")),
    )
    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert out[0].endswith("  ?")


def test_the_command_defaults_to_twenty(capsys):
    write_log(*(json.dumps(row(title=f"Show {i}")) for i in range(25)))
    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 20
    assert "Show 5 " in out[0]


def test_the_command_with_no_log_says_so(capsys):
    assert cli.main(["history"]) == 0
    assert capsys.readouterr().out == "no sessions recorded yet\n"


def test_the_command_with_an_empty_log_says_so(capsys):
    write_log()
    assert cli.main(["history"]) == 0
    assert capsys.readouterr().out == "no sessions recorded yet\n"


@pytest.mark.parametrize("n", ["0", "-3", "two", "1.5"])
def test_n_must_be_a_positive_whole_number(n, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["history", "-n", n])
    assert exc.value.code == 2
    assert "must be a whole number of 1 or more" in capsys.readouterr().err
