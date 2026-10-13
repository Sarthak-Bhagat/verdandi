"""`library_roots`: the folders `scrobd watch` records, read from config.toml."""

import pytest

from scrobd import config
from scrobd.paths import config_dir


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def write(text):
    path = config_dir() / "config.toml"
    path.write_text(text)
    return path


def test_no_config_file_is_unscoped(capsys):
    assert config.library_roots() is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "text",
    ["library = []\n", "# nothing yet\n", ""],
    ids=["empty-list", "no-library-key", "empty-file"],
)
def test_an_empty_or_absent_library_is_unscoped(text, capsys):
    write(text)
    assert config.library_roots() is None
    assert capsys.readouterr().err == ""


def test_roots_are_normalised_with_home_expanded(tmp_path, capsys):
    write('library = ["~/Media/TV_Shows/", "/mnt/Media/./Movies//", "/mnt/Media/x/../Anime"]\n')
    assert config.library_roots() == [
        str(tmp_path / "home" / "Media" / "TV_Shows"),
        "/mnt/Media/Movies",
        "/mnt/Media/Anime",
    ]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "raw",
    [b'library = ["/mnt/Media/TV_Shows"\n', b"library = \xff\n"],
    ids=["bad-toml", "not-utf8"],
)
def test_a_malformed_config_is_unscoped_and_says_so_on_one_line(raw, capsys):
    """A broken config must never silently mean "record nothing"."""
    path = config_dir() / "config.toml"
    path.write_bytes(raw)
    assert config.library_roots() is None
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert str(path) in err


def test_a_config_that_cannot_be_read_is_unscoped_and_says_so(capsys):
    path = config_dir() / "config.toml"
    path.mkdir()  # any read of it raises OSError
    assert config.library_roots() is None
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert str(path) in err


def test_a_library_that_is_one_string_not_a_list_is_unscoped_and_says_so(capsys):
    """Read as a list, a string's characters would make `/` a root, and `/` holds everything."""
    path = write('library = "/mnt/Media/TV_Shows"\n')
    assert config.library_roots() is None
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert str(path) in err


@pytest.mark.parametrize(
    ("entry", "shown"),
    [('"Media/TV_Shows"', "Media/TV_Shows"), ('"~nosuchuser/TV"', "~nosuchuser/TV"), ("5", "5")],
    ids=["relative", "unknown-user", "not-a-string"],
)
def test_an_entry_that_is_not_an_absolute_folder_is_skipped_with_one_line(entry, shown, capsys):
    path = write(f'library = [{entry}, "/mnt/Media/Movies"]\n')
    assert config.library_roots() == ["/mnt/Media/Movies"]
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert str(path) in err
    assert shown in err


def test_every_entry_relative_is_unscoped_with_a_line_for_each(capsys):
    write('library = ["Media/TV_Shows", "Media/Movies"]\n')
    assert config.library_roots() is None
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 2
    assert "Media/TV_Shows" in err
    assert "Media/Movies" in err
