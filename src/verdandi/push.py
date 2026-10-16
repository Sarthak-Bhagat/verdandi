"""Sending the session log to the server: acknowledged, resumable, retried.

**Not a send, a delivery.** HTTP carries the lines over TCP, and the server answers
only after it has committed them, with a count for every line -- added, duplicate,
rejected. The cursor (a byte offset into `sessions.jsonl`) moves only when that answer
accounts for every line sent. Anything short of it -- a timeout, a 5xx, an answer that
does not add up -- leaves the cursor where it was, and the lines go again later. The
server keys rows by session id, so sending twice is harmless: at-least-once into an
idempotent receiver.

**As soon as possible, without touching the watch.** `Pusher` runs on its own thread:
`watch` tells it a row was written and carries on reading mpv. When mpv closes, the
watcher gives it a moment to finish; if anything is still unsent it *lingers* --
retrying with backoff from 10 s up to every 2 minutes, for at most 12 hours, one
lingering process at a time -- and exits the moment everything is acknowledged. So
nothing runs while there is nothing to send.

**One notice.** When more than three rows sit unconfirmed, one desktop notification
says how many -- "verdandi: 5 watches unsent" -- and no other until the backlog has
cleared and built up again.

**The token is a file of its own**, `token` beside `config.toml`, so the config can be
shown or shared without it. It goes in the `Authorization` header and nowhere else:
never into a log line, an error message or a notification. That is why a redirect is
refused rather than followed -- urllib would carry the header to wherever it pointed --
and why a ValueError's text never reaches a message: http.client quotes the header
value it refuses, and that value is the token.

**The log is append-only, and that is assumed.** The cursor is a byte offset, so the
one replacement it notices is a log now shorter than the cursor, which is sent again
from the start. A replaced log as long as the cursor or longer is not supported: the
lines before the cursor are taken as sent. That is acceptable because nothing here
rewrites the log -- only a hand edit could -- and the cost of being wrong is one
person's rows to resend with the cursor file deleted, into a server that de-duplicates.
"""

import contextlib
import fcntl
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable

from . import _log, sessions
from .paths import config_dir, state_dir

# Lines a request: the server refuses more than 1000 with a 413.
BATCH = 200
# One request, connect to answer. The server is on the same network and answers
# after one SQLite commit; ten seconds is slow enough to mean it is not answering.
TIMEOUT_S = 10.0
FIRST_RETRY_S = 10.0
MAX_RETRY_S = 120.0
GIVE_UP_S = 12 * 3600
NOTIFY_ABOVE = 3

_CURSOR = "push.cursor"
_LOCK = "push.lock"
_NOTIFIED = "push.notified"


class PushError(Exception):
    """A push that was not fully acknowledged. The cursor did not move past it."""


def token() -> str | None:
    """Return the push token from the config directory, or None if there is none.

    A file others can read is still used, with a line on stderr: refusing it would
    stop every push over a permission, and the watcher's stderr is read.

    A token with anything but printable ASCII in it -- a second line, a control
    character -- is refused as if missing, and the line on stderr names the file and
    never the contents. A header cannot carry it, and http.client quotes the value
    it refuses.
    """
    file = config_dir() / "token"
    try:
        text = file.read_text(encoding="utf-8").strip()
        mode = file.stat().st_mode
    except (OSError, UnicodeDecodeError):
        return None
    if mode & 0o077:
        sys.stderr.write(f"verdandi: {file} can be read by others; `chmod 600` it\n")
    if not (text.isascii() and text.isprintable()):
        sys.stderr.write(
            f"verdandi: {file} holds something other than one line of printable ASCII, "
            "so it is not used and nothing is pushed\n"
        )
        return None
    return text or None


def read_cursor() -> int:
    """Return how far into the session log the server has acknowledged, in bytes."""
    try:
        return max(0, int((state_dir() / _CURSOR).read_text()))
    except (OSError, ValueError):
        return 0


def _write_cursor(offset: int) -> None:
    """Move the cursor to *offset* with one rename.

    The temporary file is named for this process and thread. Two watchers can push at
    once, and so can a pusher that outlived its grace beside `linger`; with one shared
    name, the second rename finds the file already gone. Two writers can still move the
    cursor backwards, which costs a resend and nothing else.
    """
    p = state_dir() / _CURSOR
    tmp = p.with_name(f"{_CURSOR}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(str(offset))
    tmp.replace(p)


def _pending() -> tuple[list[tuple[str, int]], int]:
    """Return complete lines past the cursor, each with the offset just after it, and the end.

    The cursor is read before the log. The other way round, another push could
    deliver rows appended after this read and move the cursor past its end, which
    would look like a replaced log and send everything again.
    """
    pos = read_cursor()
    try:
        data = sessions.log_path().read_bytes()
    except FileNotFoundError:
        return [], 0
    if pos > len(data):
        pos = 0  # the log was replaced: send it all again; the server de-duplicates
    out: list[tuple[str, int]] = []
    while (nl := data.find(b"\n", pos)) != -1:
        line, pos = data[pos:nl], nl + 1
        if line.strip():
            out.append((line.decode("utf-8", "replace"), pos))
    return out, pos


def unconfirmed() -> int | None:
    """Return how many complete rows the server has not acknowledged, or None if unreadable."""
    try:
        return len(_pending()[0])
    except OSError:
        return None


def _opener() -> urllib.request.OpenerDirector:
    """Return an opener that follows no redirect: a 3xx is an HTTPError like any other.

    Not `urlopen`, whose redirect handler keeps the Authorization header on the
    request it makes to the new address. No proxy handler either: the token goes
    to the configured server and nowhere on the way.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.UnknownHandler(),
    ):
        opener.add_handler(handler)
    return opener


def _post(server: str, tok: str, lines: list[str]) -> dict[str, object]:
    """POST *lines* to *server*; return its answer if it accounts for every line, else raise."""
    try:
        req = urllib.request.Request(
            f"{server}/api/sessions",
            data=json.dumps({"lines": lines}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
        )
        with _opener().open(req, timeout=TIMEOUT_S) as r:
            payload = r.read()
    # URLError and HTTPError are OSErrors, and so is a timeout. A reply that is not
    # HTTP at all -- a garbled status line, a body cut short -- is an HTTPException,
    # which is not.
    except (OSError, http.client.HTTPException) as exc:
        msg = f"push to {server} failed: {exc}"
        raise PushError(msg) from exc
    # A URL that cannot be parsed, or a header http.client refuses. Its text is left
    # out: for a refused header, it is the header's value, the token.
    except ValueError as exc:
        msg = f"push to {server} failed: the request could not be made ({type(exc).__name__})"
        raise PushError(msg) from exc
    try:
        ack = json.loads(payload)
    except ValueError as exc:
        msg = f"{server} answered with something that is not JSON"
        raise PushError(msg) from exc
    if not isinstance(ack, dict) or not _accounts_for(ack, len(lines)):
        msg = f"{server} acknowledged {ack!r} for {len(lines)} lines"
        raise PushError(msg)
    return ack


def _accounts_for(ack: dict[str, object], sent: int) -> bool:
    """Whether *ack* says every one of *sent* lines was added, a duplicate or rejected."""
    counts = [ack.get(k) for k in ("added", "duplicate", "rejected")]
    return (
        ack.get("received") == sent
        and all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts)
        and sum(c for c in counts if isinstance(c, int)) == sent
    )


def _notify(count: int) -> None:
    """Put up the one desktop notification; without `notify-send`, do nothing."""
    exe = shutil.which("notify-send")
    if exe is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [
                exe,
                "--app-name=verdandi",
                f"verdandi: {count} watches unsent",
                "They go as soon as the server answers.",
            ],
            check=False,
            timeout=5,
            capture_output=True,
        )


def notify_if_backlogged() -> None:
    """Notify once if more than `NOTIFY_ABOVE` rows are unconfirmed and nobody was told yet.

    Called after every failed push, and by `watch` when a server is configured but
    the token is not there to push with. The marker is made with O_EXCL, so of two
    watchers failing together only one notifies. A delivery removes it.
    """
    count = unconfirmed()
    if count is None or count <= NOTIFY_ABOVE:
        return
    try:
        fd = os.open(state_dir() / _NOTIFIED, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:  # FileExistsError: already told. Anything else: no marker, no notice.
        return
    os.close(fd)
    _notify(count)


def push(server: str, tok: str) -> int:
    """Send every unacknowledged row; return how many were acknowledged.

    Raises PushError on the first batch not fully acknowledged, and on any local
    file -- the log, the cursor -- that cannot be read or written; batches before
    it keep their progress.
    """
    try:
        return _push(server, tok)
    except PushError:
        notify_if_backlogged()
        raise


def _push(server: str, tok: str) -> int:
    """Do `push`'s work, with a local OSError or ValueError turned into a PushError."""
    sent = 0
    try:
        lines, end = _pending()
        for i in range(0, len(lines), BATCH):
            batch = lines[i : i + BATCH]
            ack = _post(server, tok, [text for text, _ in batch])
            _write_cursor(batch[-1][1])
            sent += len(batch)
            _log.event(
                "pushed",
                lines=len(batch),
                added=ack.get("added"),
                duplicate=ack.get("duplicate"),
                rejected=ack.get("rejected"),
            )
        if not lines and end > read_cursor():
            _write_cursor(end)  # only blank lines past the cursor
    except OSError as exc:
        msg = f"push failed on a local file: {exc}"
        raise PushError(msg) from exc
    except ValueError as exc:  # its text is left out, as in `_post`
        msg = f"push failed: {type(exc).__name__}"
        raise PushError(msg) from exc
    # Everything is delivered, so the next backlog may notify again. Housekeeping: a
    # marker that cannot be removed costs that notice, not this delivery.
    with contextlib.suppress(OSError):
        (state_dir() / _NOTIFIED).unlink(missing_ok=True)
    return sent


class Pusher:
    """Pushes on its own thread whenever told a row was written, so `watch` never waits."""

    def __init__(self, server: str, tok: str) -> None:
        """Push to *server* with token *tok*; nothing runs until `start`."""
        self.server, self.token = server, tok
        self.failed = False
        self._wake = threading.Event()
        self._closing = False
        self._thread = threading.Thread(target=self._run, name="verdandi-push", daemon=True)

    def start(self) -> None:
        """Start the thread, and push any backlog straight away."""
        self._thread.start()
        self.notify()

    def notify(self) -> None:
        """Say a row was written. Returns at once."""
        self._wake.set()

    def close(self, timeout: float) -> bool:
        """Push what is left, waiting at most *timeout*; return whether all was acknowledged."""
        self._closing = True
        self._wake.set()
        self._thread.join(timeout)
        return not self._thread.is_alive() and not self.failed

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            # Read before the push, not after: a push already under way when `close`
            # was called read the log before the last row, so it cannot be the last.
            closing = self._closing
            # Failed until shown otherwise. `push` turns every failure it can foresee
            # into a PushError; a bug that still escapes ends the thread with its
            # traceback in the watcher's stderr, as a failure, and the watch lingers.
            self.failed = True
            try:
                push(self.server, self.token)
                self.failed = False
            except PushError as exc:
                _log.event("push_failed", error=str(exc))
            if closing:
                return


def linger(
    server: str,
    tok: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    give_up_s: float = GIVE_UP_S,
) -> bool:
    """Retry until everything is acknowledged; return whether it was.

    Only one process lingers: a second finds the lock taken and returns False at once,
    leaving the backlog to the first, which reads the whole log on every try anyway.
    """
    try:
        lock = (state_dir() / _LOCK).open("w")
    except OSError as exc:
        _log.event("linger_failed", error=str(exc))
        return False
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        _log.event("linger_started", unconfirmed=unconfirmed())
        start, delay = clock(), FIRST_RETRY_S
        while clock() - start < give_up_s:
            sleep(delay)
            try:
                push(server, tok)
            except PushError as exc:
                _log.event("push_failed", error=str(exc), lingering=True)
                delay = min(delay * 2, MAX_RETRY_S)
                continue
            _log.event("linger_delivered")
            return True
        _log.event("linger_gave_up", unconfirmed=unconfirmed())
        return False
