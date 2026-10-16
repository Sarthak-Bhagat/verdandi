"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _xdg_in_tmp(tmp_path, monkeypatch):
    """Point every XDG base at the test's own directory, so no test can reach the real ones.

    A test that cares about a layout sets its own and overrides this.
    """
    for var, sub in (
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_DATA_HOME", "xdg-data"),
        ("XDG_STATE_HOME", "xdg-state"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
