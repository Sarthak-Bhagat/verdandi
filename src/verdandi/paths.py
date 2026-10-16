"""XDG base directories for verdandi.

Upstream simkl-mps stored everything in ~/kavin/simkl-mps because its own
`user_subdir` setting was a bootstrap paradox and nothing ever called
initialize_paths. Resolving these from the environment on every call, with no
cached module-level constant, makes that class of bug impossible here.
"""

import os
from pathlib import Path

APP = "verdandi"


def roots() -> tuple[Path, Path, Path]:
    """Return the XDG config, data and state bases, which hold one directory per app."""
    home = Path(os.environ.get("HOME", "~")).expanduser()

    def one(env_var: str, *fallback: str) -> Path:
        root = os.environ.get(env_var)
        return Path(root) if root else home.joinpath(*fallback)

    return (
        one("XDG_CONFIG_HOME", ".config"),
        one("XDG_DATA_HOME", ".local", "share"),
        one("XDG_STATE_HOME", ".local", "state"),
    )


def _app_dir(base: Path) -> Path:
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    """Return verdandi's XDG config directory, creating it if needed."""
    return _app_dir(roots()[0])


def data_dir() -> Path:
    """Return verdandi's XDG data directory, creating it if needed."""
    return _app_dir(roots()[1])


def state_dir() -> Path:
    """Return verdandi's XDG state directory, creating it if needed."""
    return _app_dir(roots()[2])
