"""The one JSON-table-on-disk idiom that index, aliases and queue all share.

Three modules keep a single JSON table on disk, and all three read it the same
way -- a missing or half-written file degrades to an empty table rather than
raising, because a cache miss is recoverable and a crashed daemon is not -- and
write it the same way: to a sibling `.tmp`, then one rename. A reader therefore
sees either the whole old table or the whole new one, never a partial write.

What the three do *not* share is the shape of "empty": the index degrades to
`{"series": {}, "movies": {}}` and the other two to `{}`. So the default is the
caller's to supply, and the caller owns the value it passes -- it is returned
as given, not copied.

Nor do they share *who* the concurrent reader is: the daemon reads the index
and the alias table, the `review` command reads the queue. That is why the
comment naming the reader lives at each call site rather than here.
"""

import json
from collections.abc import Mapping
from pathlib import Path

JsonObject = dict[str, object]
"""A JSON object read off disk. Its values are unknown until the caller checks them."""


def read_table(p: Path, default: JsonObject) -> JsonObject:
    """Read the JSON table at *p*, degrading to *default* if missing, corrupt or not a table.

    "Corrupt" includes valid JSON of the wrong shape. `[]` parses cleanly and
    then has no `.get`, and that AttributeError escaped all the way past
    `cli._cmd_resolve` -- so the file was neither identified, nor sent, nor
    queued. Silently dropping a file is the one failure this project exists to
    prevent, and it is not worth distinguishing a half-written file from a
    hand-edited one to preserve it.
    """
    try:
        table = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    return table if isinstance(table, dict) else default


def write_atomic(p: Path, data: Mapping[str, object]) -> None:
    """Write *data* to *p* as JSON via a temporary sibling file and one rename."""
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(p)
