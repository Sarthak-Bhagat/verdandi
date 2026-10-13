"""Turning a stream of mpv property changes into one record per file."""

from scrobd.session import Accumulator, Session


def feed(acc, events):
    for name, data, now in events:
        acc.feed(name, data, now)


def taken(acc: Accumulator) -> Session:
    """Return the session `acc` just finished, failing here rather than on an attribute of None."""
    s = acc.take()
    assert s is not None
    return s


def test_one_file_becomes_one_session():
    acc = Accumulator()
    feed(
        acc,
        [
            ("path", "/m/a.mkv", 100.0),
            ("duration", 1400.0, 100.0),
            ("time-pos", 10.0, 110.0),
            ("time-pos", 700.0, 800.0),
        ],
    )
    acc.finish(900.0)
    s = taken(acc)
    assert s.path == "/m/a.mkv"
    assert s.started_at == 100.0
    assert s.ended_at == 900.0
    assert s.duration == 1400.0
    assert s.max_pos == 700.0


def test_a_new_path_closes_the_previous_session():
    acc = Accumulator()
    feed(acc, [("path", "/m/a.mkv", 100.0), ("time-pos", 50.0, 150.0), ("path", "/m/b.mkv", 200.0)])
    first = taken(acc)
    assert first.path == "/m/a.mkv"
    assert first.max_pos == 50.0
    assert first.ended_at == 200.0
    feed(acc, [("time-pos", 30.0, 250.0)])
    acc.finish(300.0)
    second = taken(acc)
    assert second.path == "/m/b.mkv"
    assert second.first_pos == 30.0  # its own, not carried over from a.mkv


def test_max_pos_survives_seeking_backwards():
    """Watching to 90% then rewinding to rewatch a scene is still a 90% watch."""
    acc = Accumulator()
    feed(
        acc,
        [
            ("path", "/m/a.mkv", 0.0),
            ("duration", 100.0, 0.0),
            ("time-pos", 90.0, 10.0),
            ("time-pos", 20.0, 20.0),
        ],
    )
    acc.finish(30.0)
    assert taken(acc).max_pos == 90.0


def test_first_pos_is_where_the_play_began():
    """A reopen at the resume point can reach the max_pos of a real watch; this tells them apart."""
    acc = Accumulator()
    feed(
        acc,
        [
            ("path", "/m/a.mkv", 0.0),
            ("time-pos", 1300.0, 1.0),
            ("time-pos", 1305.0, 6.0),
            ("time-pos", 20.0, 7.0),
        ],
    )
    acc.finish(8.0)
    s = taken(acc)
    assert s.first_pos == 1300.0
    assert s.max_pos == 1305.0


def test_pause_does_not_end_a_session():
    acc = Accumulator()
    feed(
        acc,
        [
            ("path", "/m/a.mkv", 0.0),
            ("time-pos", 10.0, 10.0),
            ("pause", True, 20.0),
            ("pause", False, 600.0),
            ("time-pos", 40.0, 610.0),
        ],
    )
    acc.finish(620.0)
    s = taken(acc)
    assert s.max_pos == 40.0
    assert s.ended_at == 620.0


def test_no_position_ever_reported_leaves_max_pos_none():
    """A missing number is a gap. Never invent progress."""
    acc = Accumulator()
    feed(acc, [("path", "/m/a.mkv", 0.0), ("duration", 1400.0, 0.0)])
    acc.finish(10.0)
    s = taken(acc)
    assert s.max_pos is None
    assert s.first_pos is None
    assert s.duration == 1400.0


def test_take_returns_none_when_nothing_has_played():
    assert Accumulator().take() is None


def test_take_is_not_repeatable():
    acc = Accumulator()
    feed(acc, [("path", "/m/a.mkv", 0.0)])
    acc.finish(5.0)
    assert acc.take() is not None
    assert acc.take() is None


def test_samples_counts_position_reports():
    acc = Accumulator()
    feed(
        acc,
        [
            ("path", "/m/a.mkv", 0.0),
            ("time-pos", 1.0, 1.0),
            ("time-pos", 2.0, 2.0),
            ("time-pos", 3.0, 3.0),
        ],
    )
    acc.finish(4.0)
    assert taken(acc).samples == 3


def ticks(pos: float, at: float, n: int, *, pos_step: float = 1.0, wall_step: float = 1.0):
    """Return *n* position reports, *pos_step* of media and *wall_step* seconds of clock apart."""
    return [("time-pos", pos + i * pos_step, at + i * wall_step) for i in range(n)]


def opened(speed: float = 1.0):
    """Return what mpv sends as the watcher subscribes: file, length, unpaused, speed."""
    return [
        ("path", "/m/a.mkv", 0.0),
        ("duration", 100.0, 0.0),
        ("pause", False, 0.0),
        ("speed", speed, 0.0),
    ]


def test_a_full_watch_at_2x_counts_every_second_of_content_and_half_the_wall():
    """Speed changes how long it took, never how much was watched."""
    acc = Accumulator()
    feed(acc, opened(speed=2.0) + ticks(0.0, 0.0, 51, pos_step=2.0))
    acc.finish(50.0, "lost")
    s = taken(acc)
    assert s.content_seconds == 100.0
    assert s.wall_seconds == 50.0
    assert (s.speed_last, s.speed_max) == (2.0, 2.0)


def test_skipping_ahead_adds_no_content():
    """Jumping to the credits reaches the end. It does not watch the episode."""
    acc = Accumulator()
    feed(acc, opened() + ticks(0.0, 0.0, 11) + [("seek", None, 10.5)] + ticks(95.0, 11.0, 6))
    acc.finish(16.0, "lost")
    s = taken(acc)
    assert s.max_pos == 100.0
    assert s.content_seconds == 15.0


def test_a_jump_mpv_never_announced_is_still_a_seek():
    """The backup rule: more media than the clock and the speed allow is a skip, not watching."""
    acc = Accumulator()
    feed(acc, opened() + ticks(0.0, 0.0, 11) + ticks(95.0, 11.0, 6))
    acc.finish(16.0, "lost")
    assert taken(acc).content_seconds == 15.0


def test_an_hour_paused_counts_as_neither_content_nor_wall():
    acc = Accumulator()
    feed(
        acc,
        opened()
        + ticks(0.0, 0.0, 11)
        + [("pause", True, 10.0), ("pause", False, 3610.0)]
        + ticks(10.0, 3610.0, 11),
    )
    acc.finish(3620.0, "lost")
    s = taken(acc)
    assert s.content_seconds == 20.0
    assert s.wall_seconds == 20.0


def test_seeking_back_counts_the_rewatched_part_again():
    """Content is media played through. A scene watched twice was played twice."""
    acc = Accumulator()
    feed(acc, opened() + ticks(0.0, 0.0, 31) + [("seek", None, 30.5)] + ticks(10.0, 31.0, 21))
    acc.finish(51.0, "lost")
    assert taken(acc).content_seconds == 50.0


def test_a_resumed_play_counts_from_where_it_began():
    """Opening at the resume point is not 1300 seconds of watching."""
    acc = Accumulator()
    feed(acc, opened() + ticks(80.0, 1.0, 11))
    acc.finish(11.0, "lost")
    s = taken(acc)
    assert s.first_pos == 80.0
    assert s.content_seconds == 10.0


def test_speed_is_recorded_as_the_last_and_the_highest():
    acc = Accumulator()
    feed(acc, [*opened(), ("speed", 2.5, 5.0), ("speed", 1.5, 10.0)])
    acc.finish(11.0, "lost")
    s = taken(acc)
    assert (s.speed_last, s.speed_max) == (1.5, 2.5)


def test_time_after_the_file_ended_counts_for_nothing():
    """Mpv idling after the last file keeps the session open; that wait is not watching."""
    acc = Accumulator()
    feed(acc, opened() + ticks(0.0, 0.0, 11) + [("end-file", "eof", 10.0), ("path", None, 10.0)])
    acc.finish(900.0, "lost")
    s = taken(acc)
    assert s.wall_seconds == 10.0
    assert s.end_reason == "eof"


def test_the_end_reason_is_mpvs_when_it_gave_one():
    """Mpv sends end-file, with its reason, before the path changes to the next file."""
    acc = Accumulator()
    feed(acc, [*opened(), ("end-file", "eof", 5.0), ("path", None, 5.0), ("path", "/m/b.mkv", 5.1)])
    first = taken(acc)
    feed(acc, [("end-file", "quit", 9.0)])
    acc.finish(9.1, "lost")
    assert (first.end_reason, taken(acc).end_reason) == ("eof", "quit")


def test_the_watchers_own_reason_only_fills_a_gap():
    acc = Accumulator()
    feed(acc, opened())
    acc.finish(5.0, "signal")
    assert taken(acc).end_reason == "signal"


def test_every_session_gets_its_own_id():
    acc = Accumulator(new_id=iter(["one", "two"]).__next__)
    feed(acc, [*opened(), ("path", "/m/b.mkv", 5.0)])
    first = taken(acc)
    acc.finish(9.0, "lost")
    assert (first.session, taken(acc).session) == ("one", "two")


def test_the_default_id_is_random():
    acc = Accumulator()
    feed(acc, [*opened(), ("path", "/m/b.mkv", 5.0)])
    first = taken(acc)
    acc.finish(9.0, "lost")
    second = taken(acc)
    assert len(first.session) == 32
    assert first.session != second.session


def skips_of(events, end: float):
    acc = Accumulator()
    feed(acc, opened() + events)
    acc.finish(end, "lost")
    return taken(acc).skips


def test_a_forward_seek_is_recorded_as_a_skip_from_where_to_where():
    assert skips_of([*ticks(0.0, 0.0, 11), ("seek", None, 10.5), *ticks(95.0, 11.0, 6)], 16.0) == (
        (10.0, 95.0),
    )


def test_a_jump_mpv_never_announced_is_a_skip_too():
    assert skips_of(ticks(0.0, 0.0, 11) + ticks(95.0, 11.0, 6), 16.0) == ((10.0, 95.0),)


def test_seeking_back_is_not_a_skip():
    assert skips_of([*ticks(0.0, 0.0, 31), ("seek", None, 30.5), *ticks(10.0, 31.0, 5)], 36.0) == ()


def test_opening_at_the_resume_point_is_not_a_skip():
    assert skips_of(ticks(80.0, 1.0, 11), 11.0) == ()


def test_a_burst_of_taps_is_one_skip():
    """Five-second taps in quick succession: one skip over the opening, not three."""
    taps = [
        ("seek", None, 10.2),
        ("time-pos", 15.0, 10.3),
        ("seek", None, 10.6),
        ("time-pos", 20.0, 10.7),
        ("seek", None, 11.0),
        ("time-pos", 25.0, 11.1),
    ]
    assert skips_of(ticks(0.0, 0.0, 11) + taps + ticks(26.0, 12.0, 5), 16.0) == ((10.0, 25.0),)


def test_skips_with_watching_between_them_stay_apart():
    events = [
        *ticks(0.0, 0.0, 11),
        ("seek", None, 10.5),
        *ticks(40.0, 11.0, 6),
        ("seek", None, 16.5),
        *ticks(80.0, 17.0, 5),
    ]
    assert skips_of(events, 21.0) == ((10.0, 40.0), (45.0, 80.0))


def test_a_tap_back_ends_a_burst():
    taps = [
        ("seek", None, 10.2),
        ("time-pos", 40.0, 10.3),
        ("seek", None, 10.6),
        ("time-pos", 5.0, 10.7),
        ("seek", None, 11.0),
        ("time-pos", 15.0, 11.1),
    ]
    assert skips_of(ticks(0.0, 0.0, 11) + taps + ticks(16.0, 12.0, 3), 14.0) == (
        (10.0, 40.0),
        (5.0, 15.0),
    )


def test_a_seek_made_while_paused_is_a_skip():
    events = [
        *ticks(0.0, 0.0, 11),
        ("pause", True, 10.0),
        ("seek", None, 20.0),
        ("time-pos", 90.0, 20.1),
        ("pause", False, 30.0),
        *ticks(90.0, 30.0, 5),
    ]
    assert skips_of(events, 35.0) == ((10.0, 90.0),)


def test_an_announced_seek_is_a_skip_even_where_the_clock_would_allow_the_advance():
    """Whatever content does not count as watched, skips account for: no gap between them."""
    assert skips_of(
        [
            *ticks(0.0, 0.0, 11),
            ("seek", None, 10.5),
            ("time-pos", 14.0, 13.0),
            *ticks(15.0, 14.0, 3),
        ],
        17.0,
    ) == ((10.0, 14.0),)


def test_a_jump_while_paused_is_a_skip_even_unannounced():
    """The backup rule while paused: no clock time is playing time, so any jump is one."""
    events = [
        *ticks(0.0, 0.0, 11),
        ("pause", True, 10.0),
        ("time-pos", 90.0, 600.0),
        ("pause", False, 610.0),
        *ticks(90.0, 610.0, 5),
    ]
    assert skips_of(events, 615.0) == ((10.0, 90.0),)
