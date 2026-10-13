"""Cheapest answer first.

The tiers are ordered by cost and by purity at once: tier 0 does no I/O, tier 1
reads one local file, tier 2 reads another. Tier 3 -- guessit plus a SIMKL
search -- is the only one needing a network and is not implemented here; an
unresolved file returns confidence "none" and goes to the review queue.
"""

from . import aliases, index
from .resolution import UNKNOWN, Resolution
from .tier0 import parse


def resolve(path: str) -> Resolution:
    """Identify the media file at *path*, cheapest tier first."""
    r = parse(path)
    if r.is_actionable:
        return r

    r = index.lookup(index.load(), path)
    if r.is_actionable:
        return r

    r = aliases.lookup(path)
    if r.is_actionable:
        return r

    return UNKNOWN  # the caller queues it; nothing is guessed and nothing is lost
