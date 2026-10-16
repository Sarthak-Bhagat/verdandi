"""XDG directory resolution: env overrides, spec-default fallback, dir creation."""

from verdandi import paths


def test_honours_xdg_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert paths.config_dir() == tmp_path / "cfg" / "verdandi"
    assert paths.data_dir() == tmp_path / "data" / "verdandi"
    assert paths.state_dir() == tmp_path / "state" / "verdandi"


def test_falls_back_to_spec_defaults(tmp_path, monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / ".config" / "verdandi"
    assert paths.data_dir() == tmp_path / ".local" / "share" / "verdandi"
    assert paths.state_dir() == tmp_path / ".local" / "state" / "verdandi"


def test_directories_are_created(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.config_dir().is_dir()


def test_never_writes_outside_xdg(tmp_path, monkeypatch):
    """Upstream simkl-mps stored files in ~/kavin. Guard against a regression."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    for d in (paths.config_dir(), paths.data_dir(), paths.state_dir()):
        rel = d.relative_to(tmp_path)
        assert rel.parts[0] in (".config", ".local"), f"{d} is outside XDG"
