"""The post-mortem log: one JSON line per decision, never fatal, off with SCROBD_LOG=0."""

import json
from pathlib import Path

import pytest

from scrobd import _log, cli, index, queue, resolver
from scrobd.paths import state_dir

TAGGED = "Spooky in Love (2026) [tvdbid-466998] - S01E02 - Episode 2.mkv"
UNKNOWN = "/m/Mystery Show/Season 01/x - S01E01 - t.mkv"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.delenv("SCROBD_LOG", raising=False)


def _file():
    return state_dir() / "scrobd.jsonl"


def _lines():
    p = _file()
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _events(kind):
    return [json.loads(x) for x in _lines() if json.loads(x)["event"] == kind]


def _one(kind):
    rows = _events(kind)
    assert len(rows) == 1, f"expected one {kind!r} event, got {len(rows)}"
    return rows[0]


# --- the four events ---


def test_resolve_logs_the_whole_resolution():
    cli.main(["resolve", TAGGED])
    e = _one("resolve")
    assert e["path"] == TAGGED
    assert e["source"] == "filename"
    assert e["confidence"] == "exact"
    assert e["ids"] == {"tvdb": 466998}
    assert e["kind"] == "episode"
    assert e["season"] == 1
    assert e["episode"] == 2
    assert e["absolute"] is None
    assert e["title"] == "Spooky in Love (2026)"


def test_every_line_carries_a_utc_timestamp_and_an_event_kind():
    cli.main(["resolve", TAGGED])
    for line in _lines():
        row = json.loads(line)
        assert row["ts"].endswith("Z")
        assert row["ts"][4] == "-"
        assert isinstance(row["event"], str)


def test_the_source_field_shows_which_tier_actually_answered():
    """The whole reason logging outside `resolve` is enough: tier 0 missed, tier 1 caught it."""
    cli.main(["resolve", TAGGED])
    assert _one("resolve")["source"] == "filename"


def test_a_tier1_hit_is_logged_as_the_index():
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
    cli.main(["resolve", "/m/TV_Shows/Untagged Show (2020)/Season 01/ep - S01E03 - x.mkv"])
    e = _one("resolve")
    assert e["source"] == "index"
    assert e["ids"] == {"tvdb": 555}


def test_a_file_nothing_could_identify_is_still_logged():
    cli.main(["resolve", UNKNOWN])
    e = _one("resolve")
    assert e["confidence"] == "none"
    assert e["kind"] == "unknown"
    assert e["ids"] == {}


def test_queued_records_the_folder_key_and_the_seen_count():
    for ep in (1, 2, 3):
        cli.main(["resolve", f"/m/Mystery Show/Season 01/x - S01E0{ep} - t.mkv"])
    rows = _events("queued")
    assert [r["folder"] for r in rows] == ["Mystery Show"] * 3
    assert [r["seen"] for r in rows] == [1, 2, 3]


def test_an_identified_file_is_never_logged_as_queued():
    cli.main(["resolve", TAGGED])
    assert _events("queued") == []


def test_alias_taught_records_the_folder_the_ids_and_the_clear():
    cli.main(["resolve", UNKNOWN])
    cli.main(["review", "--answer", "Mystery Show", "--tvdb", "4242"])
    e = _one("alias_taught")
    assert e["folder"] == "Mystery Show"
    assert e["ids"] == {"tvdb": 4242}
    assert e["kind"] == "episode"
    assert e["cleared"] is True


def test_an_answer_that_cleared_nothing_teaches_nothing_and_logs_nothing():
    """No alias was written, so there is no `alias_taught` to record."""
    cli.main(["resolve", UNKNOWN])
    cli.main(["review", "--answer", "Mystery Show ", "--tvdb", "4242"])
    assert _events("alias_taught") == []


def test_index_rebuilt_records_the_counts(tmp_path):
    s = tmp_path / "s.json"
    s.write_text(
        json.dumps(
            [
                {"id": 1, "title": "A", "tvdbId": 1, "path": "/m/TV_Shows/A"},
                {"id": 2, "title": "B", "tvdbId": 2, "path": "/m/TV_Shows/B"},
            ]
        )
    )
    m = tmp_path / "m.json"
    m.write_text(json.dumps([{"id": 1, "title": "C", "imdbId": "tt1", "path": "/m/Films/C"}]))
    cli.main(["index", "--from-json", str(s), str(m)])
    e = _one("index_rebuilt")
    assert e["series"] == 2
    assert e["movies"] == 1


# --- the header, written once, saying what the file is ---


def test_a_new_file_opens_with_a_dated_header():
    _log.event("resolve", path=TAGGED)
    first = json.loads(_lines()[0])
    assert first["event"] == "log_started"
    assert first["added"] == "2026-09-21"
    # Kept with no expiry, decided 2026-10-10: a header promising one would be a lie.
    assert "review_after" not in first
    assert "temporary" not in first
    assert "SCROBD_LOG=0" in first["note"]


def test_the_header_is_written_once_not_per_append():
    for _ in range(3):
        _log.event("resolve", path=TAGGED)
    assert len(_events("log_started")) == 1
    assert len(_lines()) == 4


# --- SCROBD_LOG ---


def test_scrobd_log_0_suppresses_everything(monkeypatch):
    monkeypatch.setenv("SCROBD_LOG", "0")
    cli.main(["resolve", TAGGED])
    cli.main(["resolve", UNKNOWN])
    cli.main(["review", "--answer", "Mystery Show", "--tvdb", "4242"])
    assert not _file().exists()


def test_logging_is_on_by_default(monkeypatch):
    monkeypatch.delenv("SCROBD_LOG", raising=False)
    cli.main(["resolve", TAGGED])
    assert _file().exists()


def test_only_the_exact_value_0_turns_it_off(monkeypatch):
    monkeypatch.setenv("SCROBD_LOG", "1")
    cli.main(["resolve", TAGGED])
    assert len(_events("resolve")) == 1


def test_suppressing_the_log_still_queues_the_file(monkeypatch):
    monkeypatch.setenv("SCROBD_LOG", "0")
    assert cli.main(["resolve", UNKNOWN]) == 1
    assert queue.count() == 1


# --- a failed write costs the line and nothing else ---


def test_an_unwritable_state_dir_does_not_break_a_resolve(monkeypatch, capsys):
    def boom():
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(_log, "state_dir", boom)
    rc = cli.main(["resolve", TAGGED])
    assert rc == 0
    assert "466998" in capsys.readouterr().out


def test_a_failed_log_does_not_stop_a_file_reaching_the_queue(monkeypatch):
    def boom():
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(_log, "state_dir", boom)
    assert cli.main(["resolve", UNKNOWN]) == 1
    assert queue.count() == 1


def test_a_full_disk_loses_the_line_not_the_run(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    # Scoped, not `monkeypatch.undo()`: that also undid the fixture's XDG
    # redirect, so the assertion below read the user's real log and passed only
    # while that log happened to hold no `resolve` line.
    with monkeypatch.context() as m:
        m.setattr(Path, "open", boom)
        _log.event("resolve", path=TAGGED)  # must not raise
    assert _file().is_relative_to(tmp_path)
    assert _events("resolve") == []


def test_a_self_referential_field_loses_the_line_not_the_run():
    loop = {}
    loop["self"] = loop
    _log.event("resolve", ids=loop)
    assert _events("resolve") == []


def test_the_resolver_itself_writes_nothing():
    """Tier 0 costs no I/O. The log lives at the caller boundary, never inside `resolve`."""
    resolver.resolve(TAGGED)
    assert not _file().exists()


# --- one file, valid JSONL, and a warning instead of rotation ---


def test_the_file_stays_valid_jsonl_across_many_appends():
    for i in range(200):
        cli.main(["resolve", f"/m/Tagged {i} [tvdbid-{1000 + i}] - S01E01 - t.mkv"])
    lines = _lines()
    assert len(lines) == 201
    assert all(isinstance(json.loads(x), dict) for x in lines)
    assert len(_events("resolve")) == 200


def test_there_is_exactly_one_log_file_and_no_rotation():
    for i in range(50):
        cli.main(["resolve", f"/m/Tagged {i} [tvdbid-{2000 + i}] - S01E01 - t.mkv"])
    assert [p.name for p in state_dir().glob("*.jsonl")] == ["scrobd.jsonl"]


def test_a_newline_inside_a_path_does_not_split_the_line():
    _log.event("resolve", path="/m/we\nird.mkv")
    lines = _lines()
    assert len(lines) == 2
    assert json.loads(lines[1])["path"] == "/m/we\nird.mkv"


def test_a_large_log_warns_once_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(_log, "_WARN_BYTES", 256)
    for i in range(20):
        _log.event("resolve", path=f"/m/{i}.mkv")
    err = capsys.readouterr().err
    assert err.count("SCROBD_LOG=0") == 1
    assert "MB" in err


def test_a_small_log_says_nothing(capsys):
    _log.event("resolve", path=TAGGED)
    assert capsys.readouterr().err == ""
