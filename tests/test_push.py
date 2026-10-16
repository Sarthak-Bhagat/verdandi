"""Sending the session log: acknowledged, resumable, retried, and never yolo."""

import fcntl
import socket
import threading

import pytest

from verdandi import push, sessions
from verdandi.paths import config_dir, state_dir

TOKEN = "test-token"  # a fixture value for a local test server, not a credential
# A made-up token http.client refuses to put in a header, and quotes when it refuses.
LEAKY = "leakcheck-one\nleakcheck-two"

# Every `notify-send` argv a test would have run.
notifications: list[list[str]] = []


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("VERDANDI_LOG", "0")
    # Never a real notification on the desktop running the tests.
    monkeypatch.setattr(push.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(push.subprocess, "run", lambda argv, **_kw: notifications.append(argv))
    notifications.clear()


def notified():
    """Return the headline of every notification put up, oldest first."""
    return [arg for argv in notifications for arg in argv if arg.startswith("verdandi:")]


def write(*lines, tail=b""):
    sessions.log_path().write_bytes(b"".join(x.encode() + b"\n" for x in lines) + tail)


def append(*lines):
    with sessions.log_path().open("ab") as f:
        f.write(b"".join(x.encode() + b"\n" for x in lines))


def dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def test_everything_after_the_cursor_is_sent_and_acknowledged(server):
    url, state = server
    write('{"a": 1}', '{"b": 2}')
    assert push.push(url, TOKEN) == 2
    assert state["batches"] == [['{"a": 1}', '{"b": 2}']]
    assert push.read_cursor() == sessions.log_path().stat().st_size


def test_a_second_push_sends_nothing_new(server):
    url, state = server
    write('{"a": 1}')
    push.push(url, TOKEN)
    assert push.push(url, TOKEN) == 0
    assert len(state["batches"]) == 1


def test_the_token_goes_as_a_bearer_header(server):
    url, state = server
    write('{"a": 1}')
    push.push(url, TOKEN)
    assert state["auth"] == [f"Bearer {TOKEN}"]


def test_a_server_that_is_down_leaves_the_cursor_alone():
    write('{"a": 1}')
    with pytest.raises(push.PushError):
        push.push(dead_url(), TOKEN)
    assert push.read_cursor() == 0


def test_a_server_error_leaves_the_cursor_alone(server):
    url, state = server
    state["mode"] = "fail"
    write('{"a": 1}')
    with pytest.raises(push.PushError):
        push.push(url, TOKEN)
    assert push.read_cursor() == 0


def test_an_answer_that_is_not_http_is_a_failed_push_not_a_crash():
    """A garbled status line raises `http.client.HTTPException`, which is not an OSError."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def answer_nonsense():
        conn, _ = srv.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"nonsense\r\n\r\n")

    threading.Thread(target=answer_nonsense, daemon=True).start()
    write('{"a": 1}')
    try:
        with pytest.raises(push.PushError):
            push.push(f"http://127.0.0.1:{srv.getsockname()[1]}", TOKEN)
    finally:
        srv.close()
    assert push.read_cursor() == 0


def test_an_answer_that_does_not_account_for_every_line_is_not_delivery(server):
    url, state = server
    state["mode"] = "short"
    write('{"a": 1}', '{"b": 2}')
    with pytest.raises(push.PushError, match="acknowledged"):
        push.push(url, TOKEN)
    assert push.read_cursor() == 0


def test_an_unfinished_last_line_waits_for_its_newline(server):
    url, state = server
    write('{"a": 1}', tail=b'{"b": ')
    push.push(url, TOKEN)
    assert state["batches"] == [['{"a": 1}']]


def test_a_replaced_log_is_sent_again_from_the_start(server):
    url, state = server
    write('{"a": 1}', '{"b": 2}')
    push.push(url, TOKEN)
    write('{"c": 3}')
    push.push(url, TOKEN)
    assert state["batches"][-1] == ['{"c": 3}']


def test_each_acknowledged_batch_moves_the_cursor(server, monkeypatch):
    url, state = server
    monkeypatch.setattr(push, "BATCH", 1)
    state["accept"] = 1
    write('{"a": 1}', '{"b": 2}')
    with pytest.raises(push.PushError):
        push.push(url, TOKEN)
    assert push.read_cursor() == len(b'{"a": 1}\n')


def test_no_request_carries_more_lines_than_the_server_takes(server):
    """The server answers 413 above 1000 lines a request."""
    url, state = server
    write(*[f'{{"n": {i}}}' for i in range(1001)])
    assert push.push(url, TOKEN) == 1001
    assert max(len(batch) for batch in state["batches"]) <= 1000


def test_two_pushes_at_once_do_not_trip_over_each_others_cursor(server, monkeypatch):
    """Two watchers, or a pusher still running beside `linger`, write the cursor together."""
    url, _state = server
    monkeypatch.setattr(push, "BATCH", 1)
    write(*[f'{{"n": {i}}}' for i in range(150)])
    errors = []

    def one():
        try:
            push.push(url, TOKEN)
        except (OSError, push.PushError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_unconfirmed_counts_complete_lines_past_the_cursor():
    write('{"a": 1}', '{"b": 2}', tail=b"{")
    assert push.unconfirmed() == 2


def test_the_pusher_pushes_when_told_and_flushes_on_close(server):
    url, state = server
    p = push.Pusher(url, TOKEN)
    p.start()
    write('{"a": 1}')
    p.notify()
    assert p.close(5.0) is True
    assert ['{"a": 1}'] in state["batches"]


def test_a_row_written_while_a_push_is_in_flight_still_goes_before_close_returns(
    server, monkeypatch
):
    """A push already under way read the log before the row; closing must not end on it."""
    url, state = server
    original = push.push
    in_flight, release = threading.Event(), threading.Event()

    def slow_first(*args):
        sent = original(*args)
        if not in_flight.is_set():
            in_flight.set()
            release.wait(5)
        return sent

    monkeypatch.setattr(push, "push", slow_first)
    p = push.Pusher(url, TOKEN)
    p.start()
    assert in_flight.wait(5)
    write('{"a": 1}')
    threading.Timer(0.2, release.set).start()
    assert p.close(5.0) is True
    assert ['{"a": 1}'] in state["batches"]


def test_a_pusher_that_could_not_deliver_says_so_on_close():
    write('{"a": 1}')
    p = push.Pusher(dead_url(), TOKEN)
    p.start()
    assert p.close(5.0) is False


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_pusher_whose_thread_died_says_so_on_close(monkeypatch):
    """A bug `push` did not foresee ends the thread, with its traceback; that is not delivery."""

    def broken(*_args):
        msg = "a bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(push, "push", broken)
    write('{"a": 1}')
    p = push.Pusher("http://127.0.0.1:1", TOKEN)
    p.start()
    assert p.close(5.0) is False


def test_linger_delivers_once_the_server_answers(server):
    url, state = server
    state["mode"] = "fail"
    write('{"a": 1}')
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            state["mode"] = "ok"

    assert push.linger(url, TOKEN, sleep=sleep, clock=lambda: 0.0) is True
    assert sleeps == [10.0, 20.0, 40.0]
    assert push.read_cursor() > 0


def test_linger_backs_off_to_two_minutes_and_gives_up(server):
    url, state = server
    state["mode"] = "fail"
    write('{"a": 1}')
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    assert push.linger(url, TOKEN, sleep=sleep, clock=lambda: now[0], give_up_s=1000) is False
    assert max(sleeps) == 120.0


def test_only_one_process_lingers(server):
    """A server that answers: without the lock, this linger would deliver and say so."""
    url, state = server
    write('{"a": 1}')
    with (state_dir() / "push.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert push.linger(url, TOKEN, sleep=lambda _s: None, clock=lambda: 0.0) is False
    assert state["batches"] == []


def test_more_than_three_unconfirmed_notifies_once():
    write(*[f'{{"n": {i}}}' for i in range(4)])
    for _ in range(3):
        with pytest.raises(push.PushError):
            push.push(dead_url(), TOKEN)
    assert notified() == ["verdandi: 4 watches unsent"]


def test_three_unconfirmed_do_not_notify():
    write(*[f'{{"n": {i}}}' for i in range(3)])
    with pytest.raises(push.PushError):
        push.push(dead_url(), TOKEN)
    assert notified() == []


def test_the_notification_rearms_once_everything_is_delivered(server):
    url, _state = server
    write(*[f'{{"n": {i}}}' for i in range(4)])
    with pytest.raises(push.PushError):
        push.push(dead_url(), TOKEN)
    push.push(url, TOKEN)
    append(*[f'{{"m": {i}}}' for i in range(4)])
    with pytest.raises(push.PushError):
        push.push(dead_url(), TOKEN)
    assert notified() == ["verdandi: 4 watches unsent"] * 2


def test_the_token_is_read_from_the_config_directory():
    assert push.token() is None
    token_file = config_dir() / "token"
    token_file.write_text(f"{TOKEN}\n")
    token_file.chmod(0o600)
    assert push.token() == TOKEN


def test_a_token_others_can_read_is_still_used_but_warned_about(capsys):
    token_file = config_dir() / "token"
    token_file.write_text(f"{TOKEN}\n")
    token_file.chmod(0o644)
    assert push.token() == TOKEN
    err = capsys.readouterr().err
    assert str(token_file) in err
    assert TOKEN not in err


def test_a_private_token_is_read_without_a_word(capsys):
    token_file = config_dir() / "token"
    token_file.write_text(f"{TOKEN}\n")
    token_file.chmod(0o600)
    push.token()
    assert capsys.readouterr().err == ""


def test_a_token_with_a_control_character_is_refused_without_being_shown(capsys):
    token_file = config_dir() / "token"
    token_file.write_text(f"{LEAKY}\n")
    token_file.chmod(0o600)
    assert push.token() is None
    err = capsys.readouterr().err
    assert str(token_file) in err
    assert "leakcheck" not in err


def test_a_token_the_request_refuses_is_repeated_nowhere(server, monkeypatch, capsys):
    """Not in the error, not on stderr, not in the post-mortem log."""
    monkeypatch.setenv("VERDANDI_LOG", "1")
    url, _state = server
    write('{"a": 1}')
    with pytest.raises(push.PushError) as caught:
        push.push(url, LEAKY)
    pusher = push.Pusher(url, LEAKY)
    pusher.start()
    assert pusher.close(5.0) is False
    assert "leakcheck" not in str(caught.value)
    assert "leakcheck" not in capsys.readouterr().err
    log = (state_dir() / "verdandi.jsonl").read_text()
    assert "push_failed" in log
    assert "leakcheck" not in log


@pytest.mark.parametrize("mode", ["notjson", "fail", "short"])
def test_no_failure_message_carries_the_token(server, mode):
    url, state = server
    state["mode"] = mode
    write('{"a": 1}')
    with pytest.raises(push.PushError) as caught:
        push.push(url, TOKEN)
    assert TOKEN not in str(caught.value)


def test_a_server_that_is_down_says_so_without_the_token():
    write('{"a": 1}')
    with pytest.raises(push.PushError) as caught:
        push.push(dead_url(), TOKEN)
    assert TOKEN not in str(caught.value)


def test_a_redirect_is_a_failed_push_and_the_token_goes_nowhere_else(server, other_server):
    url, state = server
    elsewhere, elsewhere_state = other_server
    state["mode"] = "redirect"
    state["location"] = f"{elsewhere}/api/sessions"
    write('{"a": 1}')
    with pytest.raises(push.PushError):
        push.push(url, TOKEN)
    assert elsewhere_state["auth"] == []
    assert push.read_cursor() == 0


@pytest.mark.parametrize(
    "bad", ["not a url", "http://[::1", "http://127.0.0.1:notaport", "ftp://127.0.0.1"]
)
def test_a_malformed_server_is_a_failed_push_not_a_crash(bad):
    write('{"a": 1}')
    with pytest.raises(push.PushError):
        push.push(bad, TOKEN)
    assert push.read_cursor() == 0


def test_a_session_log_that_cannot_be_read_is_a_failed_push(server):
    url, _state = server
    sessions.log_path().mkdir()  # any read of it raises OSError
    with pytest.raises(push.PushError):
        push.push(url, TOKEN)


def test_a_cursor_that_cannot_be_written_is_a_failed_push(server):
    url, _state = server
    write('{"a": 1}')
    (state_dir() / "push.cursor").mkdir()  # the rename onto it raises OSError
    with pytest.raises(push.PushError):
        push.push(url, TOKEN)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_pusher_that_cannot_read_the_log_fails_without_a_traceback(server):
    url, _state = server
    sessions.log_path().mkdir()
    pusher = push.Pusher(url, TOKEN)
    pusher.start()
    assert pusher.close(5.0) is False


def test_linger_that_cannot_read_the_log_gives_up_rather_than_crash(server):
    url, _state = server
    sessions.log_path().mkdir()
    now = [0.0]

    def sleep(s):
        now[0] += s

    assert push.linger(url, TOKEN, sleep=sleep, clock=lambda: now[0], give_up_s=60) is False


def test_a_cursor_moved_on_by_another_push_is_not_mistaken_for_a_replaced_log(server, monkeypatch):
    """Another watcher appends a row and delivers it between this push's two reads."""
    url, state = server
    write('{"a": 1}')
    push.push(url, TOKEN)
    real_read_cursor = push.read_cursor

    def another_push_lands_meanwhile():
        append('{"b": 2}')
        (state_dir() / "push.cursor").write_text(str(sessions.log_path().stat().st_size))
        return real_read_cursor()

    monkeypatch.setattr(push, "read_cursor", another_push_lands_meanwhile)
    push.push(url, TOKEN)
    assert state["batches"] == [['{"a": 1}']]


def test_close_is_not_delivery_while_the_push_thread_is_still_running(server, monkeypatch):
    """The last push went through, but the thread has not finished by the deadline."""
    url, _state = server
    second_wait, release = threading.Event(), threading.Event()

    class StuckAfterTheFirstWake:
        def __init__(self):
            self.waits = 0

        def wait(self, _timeout=None):
            self.waits += 1
            if self.waits > 1:
                second_wait.set()
                release.wait(5)
            return True

        def set(self):
            pass

        def clear(self):
            pass

    pusher = push.Pusher(url, TOKEN)
    monkeypatch.setattr(pusher, "_wake", StuckAfterTheFirstWake())
    pusher.start()
    assert second_wait.wait(5)  # the first push succeeded; the thread waits again
    try:
        assert pusher.close(0.1) is False
    finally:
        release.set()


def test_blank_lines_are_not_sent(server):
    url, state = server
    write('{"a": 1}', "", "   ", '{"b": 2}')
    assert push.push(url, TOKEN) == 2
    assert state["batches"] == [['{"a": 1}', '{"b": 2}']]


def test_only_blank_lines_past_the_cursor_still_move_it(server):
    url, state = server
    write('{"a": 1}')
    push.push(url, TOKEN)
    append("", "  ")
    assert push.push(url, TOKEN) == 0
    assert push.read_cursor() == sessions.log_path().stat().st_size
    assert len(state["batches"]) == 1


def test_the_retry_limits_are_the_ones_designed():
    """12 hours of retries; a request may take 10 s, as long as the first retry waits."""
    assert push.GIVE_UP_S == 12 * 3600
    assert push.TIMEOUT_S == 10.0
    assert push.FIRST_RETRY_S == 10.0
    assert push.MAX_RETRY_S == 120.0
