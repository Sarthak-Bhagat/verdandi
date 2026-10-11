"""`scrobd watch` end to end, against a fake mpv."""

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

from scrobd import cli, queue, sessions
from scrobd.mpv import MpvSocket, MpvUnavailableError
from scrobd.paths import config_dir, data_dir, state_dir


def configure(*roots):
    """Make *roots* the library, in the config file `watch` reads at its start."""
    # A JSON array of strings is a TOML array of strings.
    (config_dir() / "config.toml").write_text(f"library = {json.dumps(list(roots))}\n")


def unconfigure():
    """Leave no library configured: the unscoped watch."""
    (config_dir() / "config.toml").unlink()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    # Never the real config: the owner's library roots would decide what these tests record.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("SCROBD_LOG", "0")
    # Never the real one: a test that forgets --socket must not reach a running mpv.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    # Scoped, as the owner's watch is: every file these tests play is under /m.
    configure("/m")


@pytest.fixture(autouse=True)
def unhandled_stop_signals_fail_the_test():
    """Fail the test on a SIGTERM or SIGHUP `watch` did not take over, instead of killing pytest."""
    def unhandled(signum, _frame):
        pytest.fail(f"{signal.Signals(signum).name} reached the handler `watch` should "
                    "have replaced")

    previous = {sig: signal.signal(sig, unhandled) for sig in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def serve(path, lines, ready):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    ready.set()
    conn, _ = srv.accept()
    with conn:
        conn.recv(4096)
        for line in lines:
            # bytes go out verbatim: how a line that is not valid UTF-8 is staged
            conn.sendall(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())
    srv.close()


def start_mpv(sock, lines):
    ready = threading.Event()
    threading.Thread(target=serve, args=(sock, lines, ready), daemon=True).start()
    ready.wait(2)


def run_watch(tmp_path, lines):
    sock = tmp_path / "mpvsocket"
    start_mpv(sock, lines)
    return cli.main(["watch", "--socket", str(sock)])


def test_a_played_file_becomes_a_session_row(tmp_path):
    rc = run_watch(tmp_path, [
        {"event": "property-change", "name": "path",
         "data": "/m/Show (2020) [tvdbid-99]/Season 01/x - S01E03 - t.mkv"},
        {"event": "property-change", "name": "duration", "data": 1400.0},
        {"event": "property-change", "name": "time-pos", "data": 1300.0},
    ])
    assert rc == 0
    rows = sessions.read()
    assert len(rows) == 1
    assert rows[0]["ids"] == {"tvdb": 99}
    assert rows[0]["episode"] == 3
    assert rows[0]["first_pos"] == 1300.0
    assert rows[0]["max_pos"] == 1300.0


def test_an_unidentifiable_file_is_recorded_and_queued(tmp_path):
    rc = run_watch(tmp_path, [
        {"event": "property-change", "name": "path", "data": "/m/Mystery/Season 01/x.mkv"},
        {"event": "property-change", "name": "time-pos", "data": 60.0},
    ])
    assert rc == 0
    assert len(sessions.read()) == 1          # recorded regardless
    assert queue.count() == 1                 # and queued for an answer


def test_a_dead_socket_exits_nonzero_with_a_reason(tmp_path, capsys):
    dead = tmp_path / "mpvsocket"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(dead))
    s.close()
    rc = cli.main(["watch", "--socket", str(dead)])
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "nothing listening" in err


def test_a_missing_socket_exits_nonzero_with_a_reason(tmp_path, capsys):
    rc = cli.main(["watch", "--socket", str(tmp_path / "absent")])
    assert rc == 1
    assert "no socket" in capsys.readouterr().err.lower()


SHOW = "/m/Show (2020) [tvdbid-99]/Season 01/x - S01E{:02d} - t.mkv"


def play(path, pos):
    return [{"event": "property-change", "name": "path", "data": path},
            {"event": "property-change", "name": "time-pos", "data": pos}]


def test_every_file_in_one_run_is_its_own_session(tmp_path):
    """The accumulator holds one finished session; an untaken one is overwritten."""
    rc = run_watch(tmp_path, play(SHOW.format(1), 1300.0) + play(SHOW.format(2), 420.0))
    assert rc == 0
    assert [(r["episode"], r["max_pos"]) for r in sessions.read()] == [(1, 1300.0), (2, 420.0)]


def test_a_path_that_is_not_utf8_costs_neither_its_session_nor_the_one_before(tmp_path):
    """A filename need not be UTF-8, and mpv passes its bytes through as they are."""
    latin1 = (b'{"event": "property-change", "name": "path", '
              b'"data": "/m/Caf\xe9 (2020) [tvdbid-77]/Season 01/x - S01E02 - t.mkv"}\n')
    rc = run_watch(tmp_path, [*play(SHOW.format(1), 1300.0), latin1,
                              {"event": "property-change", "name": "time-pos", "data": 300.0}])
    assert rc == 0
    rows = sessions.read()
    assert [(r["ids"], r["episode"], r["max_pos"]) for r in rows] == [
        ({"tvdb": 99}, 1, 1300.0), ({"tvdb": 77}, 2, 300.0)]
    assert rows[1]["path"] == "/m/Caf\ufffd (2020) [tvdbid-77]/Season 01/x - S01E02 - t.mkv"


def test_a_file_mpv_reported_nothing_about_is_recorded_with_gaps(tmp_path):
    """No duration and no position are `null`, never a number standing in for one."""
    rc = run_watch(tmp_path, play(SHOW.format(3), None)[:1])
    assert rc == 0
    row = sessions.read()[0]
    assert row["duration"] is None
    assert row["first_pos"] is None
    assert row["max_pos"] is None
    assert row["samples"] == 0


def test_a_season_directory_between_episodes_is_recorded_but_not_queued(tmp_path):
    """Lazy directory mode in mpv reports `Season 02` itself as a path before its episodes.

    Unidentifiable, and queued it would ask about a show already identified.
    Nothing was played -- mpv reported no duration and no position -- so it is
    a row, with its gaps, and not a question.
    """
    season_two = "/m/Show (2020) [tvdbid-99]/Season 02"
    rc = run_watch(tmp_path, [*play(SHOW.format(12), 1300.0),
                              {"event": "property-change", "name": "path", "data": season_two},
                              *play(f"{season_two}/x - S02E01 - t.mkv", 60.0)])
    assert rc == 0
    rows = sessions.read()
    assert [(r["path"], r["episode"], r["max_pos"]) for r in rows] == [
        (SHOW.format(12), 12, 1300.0), (season_two, None, None),
        (f"{season_two}/x - S02E01 - t.mkv", 1, 60.0)]
    assert queue.count() == 0


def test_with_no_library_a_url_is_recorded_but_never_queued(tmp_path):
    """The queue keys on a library folder; an answer for a host would name every URL on it."""
    unconfigure()
    url = "https://www.youtube.com/watch?v=abc"
    rc = run_watch(tmp_path, [*play(url, 30.0),
                              {"event": "property-change", "name": "duration", "data": 212.0}])
    assert rc == 0
    assert [(r["path"], r["max_pos"]) for r in sessions.read()] == [(url, 30.0)]
    assert queue.count() == 0


TV_SHOW = "/media/TV/Show (2020) [tvdbid-99]"


@pytest.mark.parametrize("root", ["/media/TV", "/"], ids=["its-folder", "filesystem-root"])
def test_a_file_under_a_library_root_is_recorded(tmp_path, root):
    """The root itself is in the library too: lazy directory mode reports a folder as a path."""
    configure("/media/Movies", root)
    episode = f"{TV_SHOW}/Season 01/x - S01E03 - t.mkv"
    rc = run_watch(tmp_path, [{"event": "property-change", "name": "path", "data": "/media/TV"},
                              *play(episode, 1300.0)])
    assert rc == 0
    assert [(r["path"], r["ids"], r["max_pos"]) for r in sessions.read()] == [
        ("/media/TV", {}, None), (episode, {"tvdb": 99}, 1300.0)]


def logged_events(kind):
    """Return every post-mortem log line of event *kind*, in order."""
    lines = (state_dir() / "scrobd.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == kind]


@pytest.mark.parametrize("path", ["/srv/clips/Mystery/x.mkv",
                                  "https://www.youtube.com/watch?v=abc"],
                         ids=["outside-every-root", "url"])
def test_a_file_outside_the_library_is_neither_recorded_nor_queued_but_logged(
        tmp_path, monkeypatch, path):
    """Unidentifiable and played, so in the library it would be a row and a question."""
    monkeypatch.setenv("SCROBD_LOG", "1")
    configure("/media/TV")
    rc = run_watch(tmp_path, [*play(path, 60.0),
                              {"event": "property-change", "name": "duration", "data": 212.0}])
    assert rc == 0
    assert sessions.read() == []
    assert queue.count() == 0
    assert [(e["path"], e["max_pos"]) for e in logged_events("out_of_scope")] == [(path, 60.0)]
    assert logged_events("session") == []


def test_a_root_does_not_take_in_a_sibling_that_shares_its_prefix(tmp_path):
    """`/media/TV` is not `/media/TV_Old`: a string prefix alone would say it is."""
    configure("/media/TV")
    old = "/media/TV_Old/Show (2020) [tvdbid-99]/Season 01/x - S01E04 - t.mkv"
    kept = f"{TV_SHOW}/Season 01/x - S01E05 - t.mkv"
    rc = run_watch(tmp_path, play(old, 1300.0) + play(kept, 1300.0))
    assert rc == 0
    assert [r["path"] for r in sessions.read()] == [kept]


def test_the_library_check_never_asks_the_filesystem_about_a_played_path(tmp_path, monkeypatch):
    """The library is on an autofs/sshfs mount that can hang in D state, and the watcher with it.

    `stat` and `lstat` are what `exists`, `is_dir`, `realpath` and `resolve`
    come down to. Each fails here for a played path rather than reach for it.
    """
    asked = []

    def refuse_media(real):
        def guarded(path, *args, **kwargs):
            if str(path).startswith("/media/"):
                asked.append(str(path))
                raise OSError(5, "a hung mount does not answer")
            return real(path, *args, **kwargs)
        return guarded

    monkeypatch.setattr(os, "stat", refuse_media(os.stat))
    monkeypatch.setattr(os, "lstat", refuse_media(os.lstat))
    configure("/media/TV")
    kept = f"{TV_SHOW}/Season 01/x - S01E07 - t.mkv"
    rc = run_watch(tmp_path, play("/media/TV_Old/x.mkv", 60.0) + play(kept, 1300.0))
    assert asked == []
    assert rc == 0
    assert [r["path"] for r in sessions.read()] == [kept]


def test_with_no_library_every_file_is_recorded_and_stderr_says_so(tmp_path, capsys):
    """One line, in `watch.stderr` where the owner will see it, naming the file to write."""
    unconfigure()
    clip = "/srv/clips/Mystery/x.mkv"
    rc = run_watch(tmp_path, play(clip, 60.0))
    assert rc == 0
    assert [r["path"] for r in sessions.read()] == [clip]
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert str(config_dir() / "config.toml") in err
    assert "every local file" in err


def test_a_relative_path_from_mpv_is_resolved_against_the_watchers_working_directory(
        tmp_path, monkeypatch):
    """`mpv x.mkv` reports `x.mkv`. The watcher inherits mpv's directory, so it resolves there.

    Resolved, the folder names identify the episode; left relative, neither the
    resolver nor the library could see them.
    """
    configure(str(tmp_path / "TV"))
    show = tmp_path / "TV" / "Show (2020) [tvdbid-99]"
    show.mkdir(parents=True)
    monkeypatch.chdir(show)
    rc = run_watch(tmp_path, play("Season 01/x - S01E06 - t.mkv", 1300.0))
    assert rc == 0
    assert [(r["path"], r["ids"], r["episode"]) for r in sessions.read()] == [
        (f"{show}/Season 01/x - S01E06 - t.mkv", {"tvdb": 99}, 6)]


def test_ctrl_c_records_the_session_being_watched(tmp_path, monkeypatch):
    """Losing the file you were watching because you pressed Ctrl-C is the loss to prevent."""
    stream = MpvSocket.events

    def then_ctrl_c(self):
        yield from stream(self)
        raise KeyboardInterrupt

    monkeypatch.setattr(MpvSocket, "events", then_ctrl_c)
    try:
        rc = run_watch(tmp_path, play(SHOW.format(4), 700.0))
    except KeyboardInterrupt:
        pytest.fail("Ctrl-C escaped `watch` instead of ending it")
    assert rc == 0
    assert [(r["max_pos"], r["end_reason"]) for r in sessions.read()] == [(700.0, "signal")]


def test_mpvs_own_end_reason_is_recorded(tmp_path):
    rc = run_watch(tmp_path, [*play(SHOW.format(4), 700.0),
                              {"event": "end-file", "reason": "quit"}])
    assert rc == 0
    assert [r["end_reason"] for r in sessions.read()] == ["quit"]


def test_mpv_gone_without_a_reason_is_recorded_as_lost(tmp_path):
    """The socket closed with no end-file before it: mpv crashed or was killed."""
    rc = run_watch(tmp_path, play(SHOW.format(4), 700.0))
    assert rc == 0
    assert [r["end_reason"] for r in sessions.read()] == ["lost"]


def test_every_session_is_logged_with_its_row_and_whether_it_was_written(tmp_path, monkeypatch):
    """A replay that adds no progress is deduplicated: logged, but not written."""
    monkeypatch.setenv("SCROBD_LOG", "1")
    rc = run_watch(tmp_path, play(SHOW.format(1), 1300.0) + play(SHOW.format(2), 420.0)
                   + play(SHOW.format(1), 600.0))
    assert rc == 0
    logged = [json.loads(line) for line in (state_dir() / "scrobd.jsonl").read_text().splitlines()]
    seen = [e for e in logged if e["event"] == "session"]
    assert [e["written"] for e in seen] == [True, True, False]
    rows = sessions.read()
    assert len(rows) == 2
    assert {k: v for k, v in seen[0].items() if k not in {"event", "written"}} == rows[0]


def rows_on_stderr(err):
    """Return the session rows printed to stderr, in order; every other line is prose."""
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def test_a_session_log_that_cannot_be_written_keeps_every_row_and_the_watch_carries_on(
        tmp_path, capsys):
    """Each row goes to stderr, so every session survives the failed writes.

    Carrying on, because the watcher runs detached: stopping would lose every
    later file in that mpv, where carrying on loses none of them. The exit code
    still says that something was not recorded.
    """
    (state_dir() / "sessions.jsonl").mkdir()          # any write to it raises OSError
    rc = run_watch(tmp_path, play(SHOW.format(5), 1300.0) + play(SHOW.format(6), 420.0))
    assert rc == 1
    err = capsys.readouterr().err
    assert "sessions.jsonl" in err.splitlines()[0]
    assert [(r["path"], r["max_pos"], r["ids"]) for r in rows_on_stderr(err)] == [
        (SHOW.format(5), 1300.0, {"tvdb": 99}), (SHOW.format(6), 420.0, {"tvdb": 99})]


def test_a_queue_that_cannot_be_written_keeps_the_row_and_the_watch_carries_on(
        tmp_path, capsys):
    """The queue's temporary file has a fixed name, so two writers can collide on it."""
    (state_dir() / "review.tmp").mkdir()              # the queue's write cannot land
    mystery = "/m/Mystery/Season 01/x.mkv"
    rc = run_watch(tmp_path, play(mystery, 60.0) + play(SHOW.format(7), 900.0))
    assert rc == 1
    assert [(r["path"], r["max_pos"]) for r in sessions.read()] == [
        (mystery, 60.0), (SHOW.format(7), 900.0)]
    err = capsys.readouterr().err
    assert "review queue" in err.splitlines()[0]
    assert [r["path"] for r in rows_on_stderr(err)] == [mystery]


def test_an_unforeseen_error_puts_every_session_in_hand_on_stderr_before_it_ends_the_watch(
        tmp_path, capsys):
    """It still ends the watch, but not with a session lost without a word.

    A corrupt alias table is one such error: the resolver reads the table for
    a file its name cannot identify, and undecodable bytes raise from there.
    Here that file is the one in hand -- taken, not yet written -- and the next
    file has just opened.
    """
    (data_dir() / "aliases.json").write_bytes(b"\xff")
    mystery = "/m/Mystery/Season 01/x.mkv"
    with pytest.raises(UnicodeDecodeError):
        run_watch(tmp_path, [*play(SHOW.format(1), 1300.0), *play(mystery, 60.0),
                             {"event": "property-change", "name": "path",
                              "data": SHOW.format(2)}])
    assert [r["max_pos"] for r in sessions.read()] == [1300.0]  # written before the error
    salvaged = rows_on_stderr(capsys.readouterr().err)
    assert [(r["path"], r["max_pos"]) for r in salvaged] == [
        (mystery, 60.0), (SHOW.format(2), None)]


def test_the_default_socket_is_mpvsocket_in_the_runtime_dir(tmp_path, monkeypatch):
    """Read when the command runs: the env is set here, long after `cli` was imported."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    start_mpv(tmp_path / "mpvsocket", play(SHOW.format(6), 900.0))
    assert cli.main(["watch"]) == 0
    assert [r["max_pos"] for r in sessions.read()] == [900.0]


@pytest.mark.parametrize("runtime_dir", [None, ""], ids=["unset", "empty"])
def test_no_runtime_dir_and_no_socket_refuses_rather_than_guess(runtime_dir, monkeypatch,
                                                                capsys):
    """No fallback to /tmp: a shared path another user can bind is the wrong place to watch."""
    if runtime_dir is None:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)

    def connect(self):
        pytest.fail(f"watch guessed a socket and connected to {self.path}")

    monkeypatch.setattr(MpvSocket, "connect", connect)
    assert cli.main(["watch"]) == 1
    err = capsys.readouterr().err
    assert "XDG_RUNTIME_DIR is unset" in err
    assert "--socket" in err


def test_the_help_names_the_default_socket(capsys):
    with pytest.raises(SystemExit):
        cli.main(["watch", "--help"])
    assert "$XDG_RUNTIME_DIR/mpvsocket" in capsys.readouterr().out


STOP_SIGNALS = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
STOP_IDS = ["SIGINT", "SIGTERM", "SIGHUP"]


def watch_through(tmp_path, lines):
    """Run `run_watch`; fail the test, rather than abort pytest, if an interrupt escapes."""
    try:
        return run_watch(tmp_path, lines)
    except KeyboardInterrupt:
        pytest.fail("an interrupt escaped `watch` instead of ending it")


def signal_while_resolving(monkeypatch, sig, path):
    """Send *sig* while *path* is being recorded: taken and resolved, not yet written."""
    real = cli.resolve_one

    def resolve_then_signal(p):
        r = real(p)
        if p == path:
            signal.raise_signal(sig)
        return r

    monkeypatch.setattr(cli, "resolve_one", resolve_then_signal)


@pytest.mark.parametrize("sig", STOP_SIGNALS, ids=STOP_IDS)
def test_a_signal_while_a_session_is_being_recorded_waits_for_the_write(
        tmp_path, monkeypatch, sig):
    """File 1 is in hand -- taken when file 2 opened -- so it is written before the watch stops."""
    signal_while_resolving(monkeypatch, sig, SHOW.format(1))
    rc = watch_through(tmp_path, play(SHOW.format(1), 1300.0) + play(SHOW.format(2), 420.0))
    assert rc == 0
    # The watch then stops at once, before file 2's position is read: file 2 is
    # recorded as far as it got. Reading on to 420 would mean the signal was ignored.
    assert [(r["episode"], r["max_pos"]) for r in sessions.read()] == [(1, 1300.0), (2, None)]


def test_a_signal_while_the_final_session_is_being_recorded_waits_for_the_write(
        tmp_path, monkeypatch):
    """A second Ctrl-C, landing on the last record of the watch, must not cost that record."""
    signal_while_resolving(monkeypatch, signal.SIGINT, SHOW.format(9))
    assert watch_through(tmp_path, play(SHOW.format(9), 500.0)) == 0
    assert [r["max_pos"] for r in sessions.read()] == [500.0]


@pytest.mark.parametrize("sig", STOP_SIGNALS, ids=STOP_IDS)
def test_a_signal_while_waiting_for_mpv_records_the_file_being_watched(
        tmp_path, monkeypatch, sig):
    """SIGTERM is what arrives in use: the watcher has no terminal, and logout ends it."""
    stream = MpvSocket.events

    def then_signal(self):
        events = stream(self)
        yield next(events)          # the file
        yield next(events)          # its position; then nothing, as when mpv is paused
        signal.raise_signal(sig)
        pytest.fail(f"{sig.name} did not interrupt the wait for mpv")

    monkeypatch.setattr(MpvSocket, "events", then_signal)
    rc = watch_through(tmp_path, play(SHOW.format(7), 900.0))
    assert rc == 0
    assert [r["max_pos"] for r in sessions.read()] == [900.0]


def test_a_watch_puts_the_previous_signal_handlers_back(tmp_path):
    """The handlers are the watch's only while it runs; any other command keeps its own."""
    before = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    assert run_watch(tmp_path, play(SHOW.format(8), 60.0)) == 0
    assert {sig: signal.getsignal(sig) for sig in STOP_SIGNALS} == before


def test_a_subscription_that_fails_closes_the_socket(tmp_path, monkeypatch, capsys):
    """`connect` can fail after the socket is open: mpv exiting mid-subscribe."""
    opened = []

    def mpv_exits_while_subscribing(self, _payloads):
        opened.append(self._sock)
        msg = f"{self.path} closed while subscribing -- did mpv just exit?"
        raise MpvUnavailableError(msg)

    monkeypatch.setattr(MpvSocket, "_send", mpv_exits_while_subscribing)
    assert run_watch(tmp_path, []) == 1
    assert "closed while subscribing" in capsys.readouterr().err
    assert opened[0].fileno() == -1         # closed, not left for the garbage collector


def unread(conn):
    """Return how many bytes sent on *conn* its peer has not read yet."""
    return int.from_bytes(fcntl.ioctl(conn, termios.TIOCOUTQ, bytes(4)), sys.byteorder)


def asleep_reading_a_socket(pid):
    """Whether process *pid* is blocked in a read on a unix stream socket."""
    try:
        return Path(f"/proc/{pid}/wchan").read_text().startswith("unix_stream_read")
    except OSError:
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc and a Linux ioctl")
def test_sigterm_ends_a_separate_watcher_asleep_in_a_real_read(tmp_path):
    """The detached case, for real: its own process, blocked in `recv`, ended by `kill`.

    The tests above raise the signal from Python, between reads. This one
    lands it inside the system call, the way logout does to a paused film.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.settimeout(10)                    # a watcher that never connects fails, not hangs
    srv.bind(str(tmp_path / "mpvsocket"))
    srv.listen(1)
    # The socket path is relative to `cwd`, so the argv is all literals: nothing
    # in it comes from outside this test.
    with srv, subprocess.Popen([sys.executable, "-m", "scrobd.cli", "watch",
                                "--socket", "mpvsocket"],
                               cwd=tmp_path, stderr=subprocess.PIPE) as watcher:
        try:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(10)
                conn.recv(4096)                             # the subscription
                conn.sendall(b"".join((json.dumps(e) + "\n").encode()
                                      for e in play(SHOW.format(10), 900.0)))
                # Then nothing, as from a paused film. Wait until all of it has
                # been read and the watcher is asleep in its next read.
                deadline = time.monotonic() + 10
                while not (unread(conn) == 0 and asleep_reading_a_socket(watcher.pid)):
                    assert time.monotonic() < deadline, "the watcher never blocked reading"
                    time.sleep(0.01)
                os.kill(watcher.pid, signal.SIGTERM)
                _, err = watcher.communicate(timeout=10)
        finally:
            if watcher.poll() is None:
                watcher.kill()              # a failing test leaves no watcher behind
    assert (watcher.returncode, err) == (0, b"")
    assert [(r["episode"], r["max_pos"]) for r in sessions.read()] == [(10, 900.0)]


def test_a_watch_starting_copies_trakt_scrobblers_new_scrobbles(tmp_path):
    """Its log rotates within weeks; each watch tops up the copy, so a month survives."""
    log = tmp_path / "d" / "trakt-scrobbler" / "trakt_scrobbler.log"
    log.parent.mkdir(parents=True)
    log.write_text("2026-10-12 21:00:00,1 - INFO - Thread-1 - scrobbler - "
                   "Scrobble stop successful for Some Show S02E05 at 93.50%\n")
    assert run_watch(tmp_path, play(SHOW.format(4), 700.0)) == 0
    copied = (state_dir() / "trakt-scrobbler.jsonl").read_text().splitlines()
    assert [json.loads(x)["episode"] for x in copied] == [5]
