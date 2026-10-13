"""Tier 2: the learned alias table, keyed on folder, answered once per series."""

import pytest

from scrobd import aliases
from scrobd.paths import data_dir


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_answer_once_then_every_episode_inherits_it():
    aliases.remember("Some Sideloaded Show", {"tvdb": 999}, "episode", "Some Sideloaded Show")
    r = aliases.lookup("/x/Some Sideloaded Show/Season 02/whatever - S02E07 - t.mkv")
    assert r.ids == {"tvdb": 999}
    assert (r.season, r.episode) == (2, 7)
    assert r.confidence == "high"
    assert r.source == "alias"


def test_future_seasons_inherit_too():
    """The queue is per-series. A new season must not ask again."""
    aliases.remember("Some Sideloaded Show", {"tvdb": 999}, "episode", "S")
    r = aliases.lookup("/x/Some Sideloaded Show/Season 05/x - S05E01 - t.mkv")
    assert r.ids == {"tvdb": 999}
    assert r.season == 5


def test_episode_numbers_still_come_from_the_filename():
    """The alias identifies the series; only the filename says which episode."""
    aliases.remember("Some Sideloaded Show", {"tvdb": 999}, "episode", "Some Sideloaded Show")
    r = aliases.lookup("/x/Some Sideloaded Show/Season 02/x.mkv")
    assert r.confidence == "none"


def test_unknown_folder_is_unresolved():
    assert aliases.lookup("/x/Never Seen/Season 01/x - S01E01 - t.mkv").confidence == "none"


def test_movie_alias():
    aliases.remember("A Film Folder", {"imdb": "tt1"}, "movie", "A Film")
    r = aliases.lookup("/x/A Film Folder/film.mkv")
    assert r.kind == "movie"
    assert r.ids == {"imdb": "tt1"}


def test_persists_across_processes():
    """The daemon and `scrobd review` are separate processes; disk is the only channel."""
    aliases.remember("Persisted", {"tvdb": 7}, "episode", "P")
    assert aliases.lookup("/x/Persisted/Season 01/x - S01E01 - t.mkv").ids == {"tvdb": 7}


def test_corrupt_file_degrades_to_empty():
    (data_dir() / "aliases.json").write_text("{broken")
    assert aliases.lookup("/x/Anything/Season 01/x - S01E01 - t.mkv").confidence == "none"


# --- audit 2026-09-21: `..` in the walk, wrong-shape JSON, malformed entries ---


def test_a_cancelled_out_ancestor_does_not_match():
    """The real directory here is /x/staging/incoming, which was never taught."""
    aliases.remember("Some Sideloaded Show", {"tvdb": 999}, "episode", "S")
    r = aliases.lookup("/x/Some Sideloaded Show/../staging/incoming/f - S01E01 - t.mkv")
    assert r.confidence == "none"


def test_a_dot_segment_does_not_stop_a_real_match():
    aliases.remember("Some Sideloaded Show", {"tvdb": 999}, "episode", "S")
    r = aliases.lookup("/x/./Some Sideloaded Show/Season 02/x - S02E07 - t.mkv")
    assert r.ids == {"tvdb": 999}


def test_a_resolution_is_not_an_absolute_number():
    aliases.remember("Some Sideloaded Show", {"tvdb": 999}, "episode", "S")
    r = aliases.lookup("/x/Some Sideloaded Show/Season 02/x - S02E07 - 1080 - t [WEBDL-1080p].mkv")
    assert r.absolute is None


def test_json_of_the_wrong_shape_degrades_to_empty():
    """A list parses fine and then has no .get -- the exception escaped the whole resolver."""
    (data_dir() / "aliases.json").write_text("[]")
    assert aliases.lookup("/x/Anything/Season 01/x - S01E01 - t.mkv").confidence == "none"


def test_a_malformed_entry_is_skipped_not_crashed_on():
    (data_dir() / "aliases.json").write_text(
        '{"Bad": "not an entry", "Worse": {"kind": "episode"}, '
        '"Good": {"ids": {"tvdb": 5}, "kind": "episode", "title": "Good"}}'
    )
    assert aliases.lookup("/x/Bad/Season 01/x - S01E01 - t.mkv").confidence == "none"
    assert aliases.lookup("/x/Worse/Season 01/x - S01E01 - t.mkv").confidence == "none"
    assert aliases.lookup("/x/Good/Season 01/x - S01E01 - t.mkv").ids == {"tvdb": 5}


def test_an_entry_whose_ids_are_not_library_ids_is_skipped():
    """An id is an int or a string. A list, a float or a bool is not one the resolver wrote."""
    (data_dir() / "aliases.json").write_text(
        '{"List": {"ids": {"tvdb": [5]}, "kind": "episode"}, '
        '"Bool": {"ids": {"tvdb": true}, "kind": "episode"}}'
    )
    assert aliases.lookup("/x/List/Season 01/x - S01E01 - t.mkv").confidence == "none"
    assert aliases.lookup("/x/Bool/Season 01/x - S01E01 - t.mkv").confidence == "none"
