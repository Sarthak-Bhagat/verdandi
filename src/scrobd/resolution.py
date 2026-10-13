"""What the resolver returns.

Library ids only -- tvdb, imdb, tmdb. Converting to a SIMKL or MAL id is the
sink's job, which keeps the resolver testable with no network and makes a
second sink cost nothing here.
"""

from dataclasses import dataclass
from typing import Literal

Ids = dict[str, int | str]
"""Library ids by service: `tvdb` and `tmdb` are ints, `imdb` is a string (`tt...`)."""

Kind = Literal["episode", "movie", "unknown"]
Confidence = Literal["exact", "high", "low", "none"]
Source = Literal["filename", "index", "alias", "search"]


@dataclass(frozen=True)
class Resolution:
    """The identity of one media file, in library ids."""

    kind: Kind
    ids: Ids
    season: int | None
    episode: int | None
    absolute: int | None
    title: str
    confidence: Confidence
    source: Source

    def __post_init__(self) -> None:
        """Reject identities that are internally inconsistent."""
        if self.kind == "episode" and (self.season is None or self.episode is None):
            msg = "an episode needs both season and episode"
            raise ValueError(msg)
        if self.kind == "movie" and (self.season is not None or self.episode is not None):
            msg = "a movie carries no season or episode"
            raise ValueError(msg)
        if self.kind == "unknown" and (self.season is not None or self.episode is not None):
            msg = "an unknown identity carries no season or episode"
            raise ValueError(msg)

    @property
    def is_actionable(self) -> bool:
        """Whether this may be sent to a sink without asking first."""
        return self.confidence in ("exact", "high")


def ids_from(value: object) -> Ids | None:
    """Return *value* as library ids if it is a non-empty table of them, else None.

    For tables read back off disk, which are whatever was last written there. A
    key must be a string and a value an int or a string; anything else is not
    an id this resolver could have produced, so the whole row is refused rather
    than half-trusted. `bool` is refused too, though Python counts it an int.
    """
    if not isinstance(value, dict) or not value:
        return None
    ids: Ids = {}
    for k, v in value.items():
        if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int | str):
            return None
        ids[k] = v
    return ids


UNKNOWN = Resolution(
    kind="unknown",
    ids={},
    season=None,
    episode=None,
    absolute=None,
    title="",
    confidence="none",
    source="filename",
)
