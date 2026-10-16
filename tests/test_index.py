"""Tier 1: the local index built from Sonarr/Radarr JSON, mirrored to and from disk."""

import json
from pathlib import Path

import pytest

from verdandi import index

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def idx():
    return index.build(
        json.loads((FIX / "sonarr_series.json").read_text()),
        json.loads((FIX / "radarr_movies.json").read_text()),
    )


def test_maps_a_series_folder_to_its_tvdb_id(idx):
    r = index.lookup(
        idx,
        "/data/media/TV_Shows/Spooky in Love (2026) [tvdbid-466998]/"
        "Season 01/anything at all - S01E04 - x.mkv",
    )
    assert r.ids == {"tvdb": 466998, "imdb": "tt40247521"}
    assert (r.season, r.episode) == (1, 4)
    assert r.confidence == "exact"
    assert r.source == "index"


def test_maps_a_movie_folder_to_its_imdb_id(idx):
    r = index.lookup(idx, "/data/media/Movies/Suzume (2022) [imdbid-tt16428256]/whatever.mkv")
    assert r.ids["imdb"] == "tt16428256"
    assert r.kind == "movie"


def test_unknown_folder_is_unresolved(idx):
    r = index.lookup(idx, "/data/media/TV_Shows/Not In The Index/Season 01/x - S01E01 - y.mkv")
    assert r.confidence == "none"


def test_episode_numbers_still_come_from_the_filename(idx):
    """The index identifies the series; only the filename says which episode."""
    r = index.lookup(
        idx, "/data/media/TV_Shows/Spooky in Love (2026) [tvdbid-466998]/Season 01/x.mkv"
    )
    assert r.confidence == "none"


def test_round_trips_through_disk(tmp_path, monkeypatch, idx):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = index.save(idx)
    assert p.exists()
    assert index.load() == idx


def test_load_with_no_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert index.load() == {"series": {}, "movies": {}}


def test_corrupt_index_does_not_raise(tmp_path, monkeypatch):
    """A half-written index must degrade to a cache miss, never crash the daemon."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "verdandi").mkdir(parents=True, exist_ok=True)
    (tmp_path / "verdandi" / "index.json").write_text("{not json")
    assert index.load() == {"series": {}, "movies": {}}


# --- audit 2026-09-21: `..` in the walk, wrong-shape JSON, malformed entries ---


def test_a_cancelled_out_ancestor_does_not_match(idx):
    """Pathlib keeps `..` literal, so the real directory here is /data/media/staging."""
    r = index.lookup(
        idx,
        "/data/media/TV_Shows/Spooky in Love (2026) [tvdbid-466998]/../"
        "staging/incoming/f - S01E01 - t.mkv",
    )
    assert r.confidence == "none"


def test_a_dot_segment_does_not_stop_a_real_match(idx):
    """Normalising must not break the paths that should resolve."""
    r = index.lookup(
        idx,
        "/data/media/TV_Shows/./Spooky in Love (2026) [tvdbid-466998]/Season 01/x - S01E04 - y.mkv",
    )
    assert r.ids["tvdb"] == 466998


def test_a_resolution_is_not_an_absolute_number(idx):
    r = index.lookup(
        idx,
        "/data/media/TV_Shows/Spooky in Love (2026) [tvdbid-466998]/"
        "Season 01/x - S01E04 - 1080 - y [WEBDL-1080p].mkv",
    )
    assert r.absolute is None


def test_json_of_the_wrong_shape_degrades_to_empty(tmp_path, monkeypatch):
    """Valid JSON that is not a table is a cache miss, not a traceback."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "verdandi").mkdir(parents=True, exist_ok=True)
    (tmp_path / "verdandi" / "index.json").write_text("[]")
    assert index.load() == {"series": {}, "movies": {}}


def test_a_section_of_the_wrong_shape_is_not_walked():
    r = index.lookup(
        {"series": ["Show A"], "movies": None}, "/m/Show A/Season 01/x - S01E01 - y.mkv"
    )
    assert r.confidence == "none"


def test_a_malformed_entry_is_skipped_not_crashed_on():
    """One corrupt row must not make every other series unresolvable."""
    idx = {
        "series": {"Bad": "not an entry", "Good": {"ids": {"tvdb": 5}, "title": "Good"}},
        "movies": {},
    }
    assert index.lookup(idx, "/m/Bad/Season 01/x - S01E01 - y.mkv").confidence == "none"
    assert index.lookup(idx, "/m/Good/Season 01/x - S01E01 - y.mkv").ids == {"tvdb": 5}
