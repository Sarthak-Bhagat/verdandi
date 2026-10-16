"""A minimal client for mpv's JSON IPC.

Newline-delimited JSON over a unix socket. We only ever read: observe a few
properties, then consume the property-change events mpv pushes. No playback
control, so there is nothing here that can disturb what is playing.

`$XDG_RUNTIME_DIR/mpvsocket`, in the user's own runtime directory, comes from
`input-ipc-server` in mpv.conf and discord.conf. That is a single global option
-- any user script setting it takes the channel over. mpv-discord does this, and
has left the path on disk with nothing behind it. mediactl lost a session's
watch data to that failure presenting as silence, so it raises here.
"""

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Self, TypedDict

WATCHED_PROPERTIES = ("path", "duration", "time-pos", "pause", "eof-reached", "speed")


class Change(TypedDict):
    """One thing the session needs to hear about, as `events` yields it.

    Either an observed property's new value, keyed by the property's name --
    `data` is None when mpv sent none, as for a duration before a file has
    loaded -- or one of two events, keyed by the event's name: `seek`, with no
    data, and `end-file`, with mpv's reason for it (`eof`, `stop`, `quit`,
    `error`, `redirect`). mpv has no properties by either name, so the two
    cannot collide.
    """

    name: str
    data: object


def _decode(line: bytes) -> object:
    """Return one line of mpv's output as JSON, or None if it is blank or not JSON.

    mpv passes a filename's bytes through as they are, and a filename need not
    be UTF-8. Decoding with "replace" keeps the line -- and so the path change
    that ends the session before it -- at the cost of a U+FFFD per bad byte.
    Dropping the line instead would fold the next file's numbers into the
    previous session, and "surrogateescape" leaves lone surrogates that crash
    `print` when `verdandi review` lists the path.
    """
    if not line.strip():
        return None
    try:
        return json.loads(line.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None  # never let one bad line end the stream


def _parse(line: bytes) -> Change | None:
    """Return *line* as a change the session needs, or None for anything else mpv sent."""
    msg = _decode(line)
    # Valid JSON need not be an object: `[]` parses, then has no `.get`.
    if not isinstance(msg, dict):
        return None
    match msg.get("event"):
        case "seek":
            return Change(name="seek", data=None)
        case "end-file":
            return Change(name="end-file", data=msg.get("reason"))
        case "property-change" if isinstance(name := msg.get("name"), str):
            return Change(name=name, data=msg.get("data"))
    return None


class MpvUnavailableError(Exception):
    """mpv could not be reached. Always carries why."""


class MpvSocket:
    """Read-only view of a running mpv instance."""

    def __init__(self, path: str, *, timeout: float = 5.0) -> None:
        """Point at the IPC socket at *path* without opening it.

        *timeout* bounds `connect` only. Reading is left blocking on purpose: a
        paused film reports nothing for as long as it stays paused, and a read
        timeout would end the event stream -- and with it the session -- while
        the file is still open. `events` ends when mpv closes the socket, which
        is how a watch normally ends; the caller can end it sooner with Ctrl-C,
        SIGTERM or SIGHUP.
        """
        self.path = path
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        """Open the socket and subscribe. Raise MpvUnavailableError naming the cause."""
        if not Path(self.path).exists():
            msg = f"no socket at {self.path} -- is mpv running with --input-ipc-server?"
            raise MpvUnavailableError(msg)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError as exc:
            # ConnectionRefusedError -- a socket file with nothing behind it --
            # is the case this module exists for, and is itself an OSError, so
            # one clause covers it. The message carries `exc` rather than
            # guessing which errno arrived.
            sock.close()
            msg = (
                f"{self.path} exists but nothing listening ({exc}) -- a user script "
                f"may have taken --input-ipc-server; mpv-discord does this"
            )
            raise MpvUnavailableError(msg) from exc
        sock.settimeout(None)  # reads block; see __init__ for why
        self._sock = sock
        # One write, not one per property. A subscription half-sent because mpv
        # exited mid-loop would leave us observing some properties and not
        # others, and a session missing its positions is the silent
        # under-reporting this whole module is here to prevent.
        self._send(
            [
                {"command": ["observe_property", i, prop]}
                for i, prop in enumerate(WATCHED_PROPERTIES, start=1)
            ]
        )

    def _send(self, payloads: list[dict[str, object]]) -> None:
        """Write *payloads* to mpv as newline-delimited JSON, in one call."""
        if self._sock is None:
            msg = "not connected"
            raise MpvUnavailableError(msg)
        blob = "".join(json.dumps(p) + "\n" for p in payloads).encode()
        try:
            self._sock.sendall(blob)
        except OSError as exc:
            # mpv can go away between `connect` returning and the subscription
            # landing. That must reach the caller as this exception like every
            # other unreachable-mpv case, not as a raw BrokenPipeError.
            msg = f"{self.path} closed while subscribing ({exc}) -- did mpv just exit?"
            raise MpvUnavailableError(msg) from exc

    def events(self) -> Iterator[Change]:
        """Yield what the session needs from mpv, in order, until mpv closes the socket."""
        if self._sock is None:
            self.connect()
        sock = self._sock
        if sock is None:
            # Unreachable: `connect` either sets the socket or raises. Said
            # here, the same way `_send` says it, because a type checker cannot
            # see what a method call did to an attribute.
            msg = "not connected"
            raise MpvUnavailableError(msg)
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return  # closed under us; keep what we have
            if not chunk:
                return  # mpv exited; drop any partial line
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                change = _parse(line)
                if change is not None:
                    yield change

    def close(self) -> None:
        """Close the socket. Safe on one never opened, and safe to call twice."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> Self:
        """Connect on entry, so an unreachable mpv is raised at the `with`, not later."""
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the socket, whatever happened inside the block."""
        self.close()
