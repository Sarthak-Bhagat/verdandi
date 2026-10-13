"""The resolver: tiers 0-2 tried cheapest first, tier 3 explicitly out of scope."""

import pytest

from scrobd import aliases, index, resolver


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_tier0_wins_and_costs_no_io(monkeypatch):
    def explode(*_a, **_k):
        msg = "tier 1 must not run when the filename answers"
        raise AssertionError(msg)

    monkeypatch.setattr(index, "load", explode)
    r = resolver.resolve("Spooky in Love (2026) [tvdbid-466998] - S01E02 - Episode 2.mkv")
    assert r.source == "filename"
    assert r.confidence == "exact"


def test_falls_through_to_the_index():
    index.save(
        index.build(
            [
                {
                    "id": 1,
                    "title": "Untagged Show",
                    "tvdbId": 555,
                    "path": "/m/TV_Shows/Untagged Show (2020)",
                }
            ],
            [],
        )
    )
    r = resolver.resolve("/m/TV_Shows/Untagged Show (2020)/Season 01/ep - S01E03 - x.mkv")
    assert r.source == "index"
    assert r.ids == {"tvdb": 555}


def test_tier1_wins_and_never_consults_the_alias_table(monkeypatch):
    """The mirror of tier 0's guard: an index hit must not fall through to tier 2."""
    index.save(
        index.build(
            [
                {
                    "id": 1,
                    "title": "Untagged Show",
                    "tvdbId": 555,
                    "path": "/m/TV_Shows/Untagged Show (2020)",
                }
            ],
            [],
        )
    )

    def explode(*_a, **_k):
        msg = "tier 2 must not run when the index answers"
        raise AssertionError(msg)

    monkeypatch.setattr(aliases, "lookup", explode)
    r = resolver.resolve("/m/TV_Shows/Untagged Show (2020)/Season 01/ep - S01E03 - x.mkv")
    assert r.source == "index"
    assert r.confidence == "exact"


def test_falls_through_to_an_alias():
    aliases.remember("Sideload", {"tvdb": 77}, "episode", "Sideload")
    r = resolver.resolve("/m/Other/Sideload/Season 01/x - S01E09 - y.mkv")
    assert r.source == "alias"
    assert r.episode == 9


def test_unresolvable_returns_none_confidence_not_an_exception(tmp_path):
    r = resolver.resolve(str(tmp_path / "random file.mkv"))
    assert r.confidence == "none"
    assert r.kind == "unknown"


def test_never_returns_low_confidence_in_this_plan(tmp_path):
    """Tier 3 (network search) is a later plan. Nothing here may emit `low`."""
    for p in (str(tmp_path / "x.mkv"), "Show - S01E01.mkv", "Movie (2020).mkv"):
        assert resolver.resolve(p).confidence in ("exact", "high", "none")
