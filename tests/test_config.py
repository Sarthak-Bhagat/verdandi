"""config.toml: the folders `verdandi watch` records, and the server it pushes to."""

import pytest

from verdandi import config
from verdandi.paths import config_dir


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


def test_the_server_is_read_from_the_config():
    write('server = "https://verdandi.example/"\n')
    assert config.server_url() == "https://verdandi.example"


def test_a_server_that_is_not_http_is_ignored_with_a_warning(capsys):
    path = write('server = "file:///etc/passwd"\n')
    assert config.server_url() is None
    err = capsys.readouterr().err
    assert "server" in err
    assert str(path) in err


def test_load_reads_the_library_and_the_server_together():
    write('library = ["/mnt/Media/Movies"]\nserver = "http://127.0.0.1:8000"\n')
    assert config.load() == config.Settings(
        library=["/mnt/Media/Movies"], server="http://127.0.0.1:8000"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://someone:pa55word@urd.example",
        "http://someone:pa55word@[::1",
        "ftp://someone:pa55word@urd.example",
    ],
    ids=["parses", "does-not-parse", "not-http"],
)
def test_a_server_with_a_user_or_password_is_ignored_without_repeating_it(url, capsys):
    path = write(f'server = "{url}"\n')
    assert config.server_url() is None
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert str(path) in err
    assert "pa55word" not in err


@pytest.mark.parametrize(
    "url", ["http://[::1", "http://127.0.0.1:notaport", "https://"], ids=["bracket", "port", "host"]
)
def test_a_server_urllib_cannot_use_is_ignored_with_one_line(url, capsys):
    path = write(f'server = "{url}"\n')
    assert config.server_url() is None
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert str(path) in err
