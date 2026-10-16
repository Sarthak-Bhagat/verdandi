"""Moving scrobd's directories to verdandi's: once, without forking or overwriting anything."""

import json

import pytest

from verdandi import _migrate


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Return the three XDG bases, each empty."""
    bases = {
        "config": tmp_path / "cfg",
        "data": tmp_path / "data",
        "state": tmp_path / "state",
    }
    monkeypatch.setenv("XDG_CONFIG_HOME", str(bases["config"]))
    monkeypatch.setenv("XDG_DATA_HOME", str(bases["data"]))
    monkeypatch.setenv("XDG_STATE_HOME", str(bases["state"]))
    return bases


def old(xdg, kind, name, text):
    d = xdg[kind] / "scrobd"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def log_events(xdg):
    path = xdg["state"] / "verdandi" / "verdandi.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_moves_the_files_and_leaves_no_old_directory(xdg):
    old(xdg, "state", "sessions.jsonl", "row\n")
    old(xdg, "config", "config.toml", "[library]\n")
    _migrate.adopt_old_name()
    assert (xdg["state"] / "verdandi" / "sessions.jsonl").read_text() == "row\n"
    assert (xdg["config"] / "verdandi" / "config.toml").read_text() == "[library]\n"
    assert not (xdg["state"] / "scrobd").exists()
    assert not (xdg["config"] / "scrobd").exists()


def test_the_log_file_takes_the_new_name(xdg):
    old(xdg, "state", "scrobd.jsonl", '{"event": "session"}\n')
    _migrate.adopt_old_name()
    new = xdg["state"] / "verdandi"
    assert not (new / "scrobd.jsonl").exists()
    assert '{"event": "session"}' in (new / "verdandi.jsonl").read_text()


def test_merges_into_a_directory_the_launcher_already_made(xdg):
    """The Lua's `mkdir -p` and its stderr redirect create the new directory first."""
    old(xdg, "state", "sessions.jsonl", "row\n")
    old(xdg, "state", "watch.stderr", "")
    new = xdg["state"] / "verdandi"
    new.mkdir()
    (new / "watch.stderr").write_text("")
    _migrate.adopt_old_name()
    assert (new / "sessions.jsonl").read_text() == "row\n"
    assert not (xdg["state"] / "scrobd").exists()


def test_never_overwrites_a_file_the_new_directory_already_has(xdg):
    old(xdg, "state", "sessions.jsonl", "old\n")
    new = xdg["state"] / "verdandi"
    new.mkdir()
    (new / "sessions.jsonl").write_text("new\n")
    _migrate.adopt_old_name()
    assert (new / "sessions.jsonl").read_text() == "new\n"
    assert (new / "sessions.jsonl.scrobd").read_text() == "old\n"
    assert not (xdg["state"] / "scrobd").exists()


def test_a_second_run_does_nothing(xdg):
    old(xdg, "state", "sessions.jsonl", "row\n")
    _migrate.adopt_old_name()
    before = (xdg["state"] / "verdandi" / "verdandi.jsonl").read_text()
    _migrate.adopt_old_name()
    assert (xdg["state"] / "verdandi" / "verdandi.jsonl").read_text() == before


def test_a_fresh_install_creates_nothing(xdg):
    _migrate.adopt_old_name()
    for base in xdg.values():
        assert not base.exists()


def test_the_move_is_logged_once(xdg):
    old(xdg, "state", "sessions.jsonl", "row\n")
    old(xdg, "config", "config.toml", "x")
    _migrate.adopt_old_name()
    moved = [e for e in log_events(xdg) if e["event"] == "migrated"]
    assert len(moved) == 2
    assert {tuple(e["moved"]) for e in moved} == {("sessions.jsonl",), ("config.toml",)}
