"""The review queue: files the resolver could not identify, one entry per series."""

import pytest

from verdandi import queue
from verdandi.paths import state_dir


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def test_one_entry_per_series_not_per_episode():
    for ep in range(1, 13):
        queue.add(f"/m/Unknown Show/Season 01/x - S01E{ep:02d} - t.mkv", "Unknown Show")
    assert queue.count() == 1


def test_entry_records_the_folder_and_an_example_file():
    queue.add("/m/Unknown Show/Season 01/x - S01E01 - t.mkv", "Unknown Show")
    e = queue.pending()[0]
    assert e["folder"] == "Unknown Show"
    assert e["example"].endswith("S01E01 - t.mkv")
    assert e["seen"] == 1


def test_seen_count_rises_with_repeats():
    for _ in range(3):
        queue.add("/m/Unknown Show/Season 01/x - S01E01 - t.mkv", "Unknown Show")
    assert queue.pending()[0]["seen"] == 3


def test_resolving_removes_it():
    queue.add("/m/Unknown Show/Season 01/x - S01E01 - t.mkv", "Unknown Show")
    queue.resolve_entry("Unknown Show")
    assert queue.count() == 0


def test_resolving_something_absent_is_not_an_error():
    queue.resolve_entry("Never Queued")
    assert queue.count() == 0


def test_survives_a_corrupt_file():
    (state_dir() / "review.json").write_text("{broken")
    queue.add("/m/X/Season 01/x - S01E01 - t.mkv", "X")
    assert queue.count() == 1


def test_a_series_folder_starting_with_season_is_not_mistaken_for_one():
    """A film, "Season of the Witch (2011)". A startswith() check swallowed it."""
    queue.add("/m/Movies/Season of the Witch (2011)/film.mkv", "Season of the Witch")
    assert queue.pending()[0]["folder"] == "Season of the Witch (2011)"


def test_specials_key_on_the_series_not_the_specials_folder():
    """Sonarr's specialsFolderFormat is "Specials"; every show has one."""
    queue.add("/m/TV/Dimension 20/Specials/x - S00E69 - t.mkv", "Dimension 20")
    queue.add("/m/TV/Doctor Who/Specials/y - S00E01 - t.mkv", "Doctor Who")
    assert queue.count() == 2
    assert {e["folder"] for e in queue.pending()} == {"Dimension 20", "Doctor Who"}


def test_a_file_directly_in_its_series_folder():
    queue.add("/m/TV/Unknown Show/x - S01E01 - t.mkv", "Unknown Show")
    assert queue.pending()[0]["folder"] == "Unknown Show"


# --- audit 2026-09-21: wrong-shape JSON must not invent or hide entries ---


def test_json_of_the_wrong_shape_reports_no_phantom_entries():
    """`len()` of a parsed list counted rows that are not queue entries."""
    (state_dir() / "review.json").write_text("[1, 2, 3]")
    assert queue.count() == 0
    assert queue.pending() == []


def test_a_queue_of_the_wrong_shape_still_accepts_a_new_entry():
    (state_dir() / "review.json").write_text('["nonsense"]')
    queue.add("/m/X/Season 01/x - S01E01 - t.mkv", "X")
    assert queue.count() == 1


def test_a_malformed_entry_is_not_counted_and_does_not_break_the_listing():
    (state_dir() / "review.json").write_text(
        '{"Bad": "not an entry", '
        '"Good": {"folder": "Good", "title": "G", "example": "/m/Good/x.mkv", "seen": 2}}'
    )
    assert queue.count() == 1
    assert [e["folder"] for e in queue.pending()] == ["Good"]


def test_adding_over_a_malformed_entry_replaces_it():
    (state_dir() / "review.json").write_text('{"X": "not an entry"}')
    queue.add("/m/X/Season 01/x - S01E01 - t.mkv", "X")
    assert queue.pending()[0]["seen"] == 1


def test_resolve_entry_reports_whether_it_removed_anything():
    queue.add("/m/Unknown Show/Season 01/x - S01E01 - t.mkv", "Unknown Show")
    assert queue.resolve_entry("Unknown Show") is True
    assert queue.resolve_entry("Unknown Show") is False
    assert queue.resolve_entry("Unknown Show ") is False


def test_an_entry_with_a_field_of_the_wrong_type_is_not_counted():
    """`review` prints the folder and example; an entry that cannot be printed is not one."""
    (state_dir() / "review.json").write_text(
        '{"Odd": {"folder": "Odd", "title": 5, "example": "/m/Odd/x.mkv", "seen": 1}, '
        '"Flag": {"folder": "Flag", "title": "F", "example": "/m/Flag/x.mkv", "seen": true}}'
    )
    assert queue.count() == 0
    assert queue.pending() == []
