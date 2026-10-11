"""A post-mortem trail of what scrobd decided, one JSON object per line.

Added 2026-09-21 so that a wrong scrobble can be reconstructed afterwards
rather than re-enacted. It was meant to run a week; on 2026-10-10 it was kept
with no expiry, because its `out_of_scope` lines are the only trace of a file
played outside the configured library -- a root the config misses shows up
nowhere else. `SCROBD_LOG=0` turns it off with no code change.

**Decisions, never credentials.** Every field written here is something the tool
worked out -- a path, a tier, a library id, a count. Nothing that authenticates
anything belongs in a log line. When the sink layer lands and brings Simkl and
MAL tokens with it, those live in the config file and go nowhere near this
function: the file is plain text, append-only, and read a week later with `jq`.
There is deliberately no redaction pass here to lean on. A guessed redaction
regex hides the leak rather than preventing it; the rule is that a secret is
never passed to `event` in the first place.

**Why this is not inside `resolve()`.** Tier 0 costs no I/O, and
`tests/test_resolver.py::test_tier0_wins_and_costs_no_io` holds it to that by
making `index.load` raise. A log line written from inside the resolver would
touch the disk for every file played, even when the filename alone answered. So
the resolver stays a pure function and its callers -- `cli`, and the daemon once
it exists -- log what came back. `Resolution.source` records which tier won, so
a post-mortem still sees that tier 0 missed and the index caught it.

**One file, no rotation**, because a week of history should be one `jq` away and
rotation puts half of it in a file you forget to look in. Instead of rotating,
the logger warns once on stderr when the file passes `_WARN_BYTES`.
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .paths import state_dir

_FILE = "scrobd.jsonl"
_OFF = "SCROBD_LOG"         # "0" disables; anything else, including unset, leaves it on

# The date the artefact carries about itself, so the file explains itself to
# whoever finds it without this repo open.
_ADDED = "2026-09-21"

# 5 MiB. A resolve line with a full library path runs ~500 bytes, and the
# heaviest plausible week -- 50 files a day, two events each -- is ~350 KB. So
# this is roughly fourteen weeks of that. Measured in real use, a week was
# 47 KB, so normal watching reaches it in about two years: the warning is then
# the prompt to move the file aside, and sooner it means a caller is looping.
_WARN_BYTES = 5 * 1024 * 1024

# Paths already warned about. A set mutated in place, not a rebound module
# global, so no `global` statement -- which ruff rejects (PLW0603) and this
# project does not silence.
_warned: set[str] = set()


def _now() -> str:
    """Return the current UTC instant as ISO-8601.

    Whole seconds and a `Z`, not `+00:00` with microseconds, because that is the
    spelling `jq`'s `fromdateiso8601` parses. Sub-second ordering is not lost by
    dropping the fraction: the file is append-only, so its own line order is the
    order things happened in.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header() -> dict[str, object]:
    """Return the first line of a new log file: what this file is and how to turn it off."""
    return {
        "ts": _now(),
        "event": "log_started",
        "added": _ADDED,
        "note": (f"post-mortem log of what scrobd decided, added {_ADDED}; kept with no "
                 "expiry. SCROBD_LOG=0 turns it off."),
    }


def _warn_if_large(path: Path) -> None:
    """Warn on stderr, once per path per process, if the log has grown past the threshold."""
    key = str(path)
    if key in _warned:
        return
    try:
        size = path.stat().st_size
        if size < _WARN_BYTES:
            return
        _warned.add(key)
        sys.stderr.write(
            f"scrobd: {path} has reached {size / (1024 * 1024):.0f} MB -- move it aside "
            f"to start a fresh one, or set SCROBD_LOG=0 to turn it off\n")
    except (OSError, ValueError):
        return          # a stat that fails or a closed stderr is not worth a traceback


def event(kind: str, /, **fields: object) -> None:
    """Append one JSON line describing *kind* to the log. Never raises.

    *kind* is positional-only on purpose: a `Resolution` has its own `kind`
    field, and the caller must be able to pass it as a keyword without colliding
    with this one. The event kind is written under `event` and the media kind
    under `kind`, which keeps both readable in `jq`.

    Pass decisions -- paths, tiers, ids, counts. Never pass a token; see the
    module docstring.
    """
    if os.environ.get(_OFF) == "0":
        return
    try:
        path = state_dir() / _FILE
        first = not path.exists()
        line = json.dumps({"ts": _now(), "event": kind, **fields}, default=str)
        with path.open("a", encoding="utf-8") as f:
            if first:
                f.write(json.dumps(_header()) + "\n")
            f.write(line + "\n")
    except (OSError, ValueError, TypeError):
        # Logging is a convenience; a scrobble is not. A read-only state
        # directory or a full disk raises OSError, a self-referential field
        # raises ValueError, an unserialisable one TypeError -- and every one of
        # them costs this line and nothing else. Named rather than a blind
        # `except Exception`, which is the ruff rule (BLE001) this project does
        # not silence.
        return
    else:
        _warn_if_large(path)
