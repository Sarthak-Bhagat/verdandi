"""The CLI: resolve, review and index as run from a shell."""

import json

import pytest

from verdandi import aliases, cli, queue
from verdandi.paths import data_dir, state_dir
from verdandi.resolution import Resolution


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))


def test_resolve_prints_the_identity(capsys):
    rc = cli.main(["resolve", "Spooky in Love (2026) [tvdbid-466998] - S01E02 - Episode 2.mkv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tvdb" in out
    assert "466998" in out
    assert "S01E02" in out


def test_resolve_of_an_unknown_file_exits_nonzero_and_queues_it(capsys):
    rc = cli.main(["resolve", "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"])
    assert rc == 1
    assert "could not identify" in capsys.readouterr().out.lower()
    assert queue.count() == 1


def test_review_lists_pending(capsys):
    cli.main(["resolve", "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"])
    cli.main(["review"])
    assert "Mystery Show" in capsys.readouterr().out


def test_review_with_an_answer_writes_an_alias_and_clears_the_queue():
    cli.main(["resolve", "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"])
    rc = cli.main(["review", "--answer", "Mystery Show", "--tvdb", "4242"])
    assert rc == 0
    assert queue.count() == 0
    r = cli.resolve_one("/m/Mystery Show/Season 03/x - S03E05 - t.mkv")
    assert r.ids == {"tvdb": 4242}


def test_review_answer_requires_an_id():
    cli.main(["resolve", "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"])
    rc = cli.main(["review", "--answer", "Mystery Show"])
    assert rc == 2


def test_index_from_json(tmp_path, capsys):
    s = tmp_path / "s.json"
    s.write_text(json.dumps([{"id": 1, "title": "T", "tvdbId": 1, "path": "/m/TV_Shows/T (2020)"}]))
    m = tmp_path / "m.json"
    m.write_text("[]")
    rc = cli.main(["index", "--from-json", str(s), str(m)])
    assert rc == 0
    assert "1 series" in capsys.readouterr().out


def test_index_with_a_missing_file_is_a_usage_error(tmp_path, capsys):
    """A bad path is a usage error, not a traceback -- same exit code as `review`'s."""
    missing = str(tmp_path / "nope.json")
    rc = cli.main(["index", "--from-json", missing, missing])
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "Traceback" not in err


def test_index_with_malformed_json_is_a_usage_error(tmp_path, capsys):
    s = tmp_path / "s.json"
    s.write_text("{not json")
    m = tmp_path / "m.json"
    m.write_text("[]")
    rc = cli.main(["index", "--from-json", str(s), str(m)])
    assert rc == 2
    assert "not valid JSON" in capsys.readouterr().err


# --- audit 2026-09-21: an answer that matched nothing, and low-confidence guesses ---


def test_review_answer_that_matches_nothing_fails_loudly(capsys):
    """A trailing space made a dead alias key and still reported success."""
    cli.main(["resolve", "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"])
    rc = cli.main(["review", "--answer", "Mystery Show ", "--tvdb", "4242"])
    assert rc != 0
    assert "Mystery Show " in capsys.readouterr().err
    assert queue.count() == 1


def test_an_answer_that_matched_nothing_writes_no_alias():
    """Writing under a key nothing will ever look up is how the queue silently leaked."""
    cli.main(["resolve", "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"])
    cli.main(["review", "--answer", "Mystery Show ", "--tvdb", "4242"])
    assert aliases.load() == {}


def test_resolve_queues_a_low_confidence_guess_rather_than_announcing_it(monkeypatch, capsys):
    """Tier 3 returns `low`. It must reach the queue, never a sink and never exit 0."""
    guess = Resolution(
        kind="episode",
        ids={"tvdb": 1},
        season=1,
        episode=1,
        absolute=None,
        title="Guessed Show",
        confidence="low",
        source="search",
    )
    monkeypatch.setattr(cli, "resolve_one", lambda _path: guess)
    rc = cli.main(["resolve", "/m/Guessed Show/Season 01/x - S01E01 - t.mkv"])
    assert rc == 1
    assert "could not identify" in capsys.readouterr().out.lower()
    assert queue.count() == 1


def test_a_wrong_shape_alias_file_does_not_drop_the_file_on_the_floor():
    """The escape path: aliases.lookup raised past _cmd_resolve, so queue.add never ran."""
    (data_dir() / "aliases.json").write_text("[]")
    rc = cli.main(["resolve", "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"])
    assert rc == 1
    assert queue.count() == 1


def test_compare_prints_the_window_and_fails_on_an_unexplained_difference(capsys):
    (state_dir() / "trakt-scrobbler.jsonl").write_text(
        json.dumps(
            {
                "at": "2026-10-12T21:00:00Z",
                "t": 1791838800.0,
                "kind": "scrobbled",
                "action": "stop",
                "title": "Some Show",
                "season": 2,
                "episode": 5,
                "progress": 93.5,
            }
        )
        + "\n"
    )
    assert cli.main(["compare", "1"]) == 1
    out = capsys.readouterr().out
    assert "window 1: 2026-10-11 → 2026-11-10" in out
    assert "only trakt-scrobbler" in out
    assert "Some Show S02E05" in out


def test_compare_is_clean_with_nothing_to_compare(capsys):
    assert cli.main(["compare", "1"]) == 0
    assert "unexplained: 0" in capsys.readouterr().out


def test_any_command_first_adopts_the_old_scrobd_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "state" / "scrobd").mkdir(parents=True)
    (tmp_path / "state" / "scrobd" / "sessions.jsonl").write_text("row\n")
    cli.main(["review"])
    assert (tmp_path / "state" / "verdandi" / "sessions.jsonl").read_text() == "row\n"
    assert not (tmp_path / "state" / "scrobd").exists()
