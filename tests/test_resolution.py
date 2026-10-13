"""Resolution's frozen-record invariants: episode/movie identity rules, actionability."""

import dataclasses
from typing import Any

import pytest

from scrobd.resolution import UNKNOWN, Resolution


def make(**kw):
    base: dict[str, Any] = {
        "kind": "episode",
        "ids": {"tvdb": 1},
        "season": 1,
        "episode": 1,
        "absolute": None,
        "title": "X",
        "confidence": "exact",
        "source": "filename",
    }
    base.update(kw)
    return Resolution(**base)


def test_is_frozen():
    r = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.season = 2


def test_actionable_only_for_exact_and_high():
    assert make(confidence="exact").is_actionable
    assert make(confidence="high").is_actionable
    assert not make(confidence="low").is_actionable
    assert not make(confidence="none").is_actionable


def test_unknown_is_not_actionable_and_carries_no_ids():
    assert UNKNOWN.kind == "unknown"
    assert UNKNOWN.confidence == "none"
    assert UNKNOWN.ids == {}
    assert not UNKNOWN.is_actionable


def test_episode_requires_season_and_episode():
    """A partial identity is worse than none -- it would sync the wrong episode."""
    with pytest.raises(ValueError, match="season and episode"):
        make(season=None)
    with pytest.raises(ValueError, match="season and episode"):
        make(episode=None)


def test_movie_must_not_carry_episode_numbers():
    with pytest.raises(ValueError, match="movie"):
        make(kind="movie", season=1, episode=1)


def test_unknown_must_not_carry_episode_numbers():
    """`unknown` means nothing was identified; a season number would be a half-answer."""
    with pytest.raises(ValueError, match="unknown identity"):
        make(kind="unknown", season=5, episode=3)
