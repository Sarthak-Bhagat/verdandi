"""mpv/scrobd.lua starts `scrobd watch` on mpv's own socket, once per mpv process."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

MPV = shutil.which("mpv") or ""
SCRIPT = Path(__file__).resolve().parent.parent / "mpv" / "scrobd.lua"
RESETS_SOCKET = Path(__file__).resolve().parent / "fixtures" / "resets_ipc_server.lua"
CLIP = "av://lavfi:testsrc=size=64x64:rate=5"
pytestmark = pytest.mark.skipif(not MPV, reason="mpv is not installed")


def stub_scrobd(tmp_path: Path, bin_dir: Path | None = None) -> Path:
    """Put a fake `scrobd` that appends its argv as one line and says hello on stderr.

    In *bin_dir*, or by default in the directory `play` puts first on PATH.
    Returns the file the argv goes to, which exists only once the stub has run.
    """
    bin_dir = bin_dir or tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    record = bin_dir / "argv"
    stub = bin_dir / "scrobd"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{record}"\necho stub-stderr >&2\n')
    stub.chmod(0o755)
    return record


def play(tmp_path: Path, *args: str, **overrides: str) -> None:
    """Run mpv with the launcher loaded and `tmp_path/bin` first on PATH; *overrides* win."""
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        **overrides,
    }
    subprocess.run(
        [
            MPV,
            "--no-config",
            "--vo=null",
            "--ao=null",
            "--frames=3",
            "--idle=no",
            "--keep-open=no",
            f"--script={SCRIPT}",
            *args,
        ],
        check=True,
        capture_output=True,
        timeout=30,
        env=env,
        cwd=tmp_path,
    )


def wait_for(path: Path, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_it_starts_scrobd_watch_on_mpvs_own_socket(tmp_path):
    record = stub_scrobd(tmp_path)
    sock = tmp_path / "mpv.sock"
    play(tmp_path, f"--input-ipc-server={sock}", CLIP)
    assert wait_for(record)
    assert record.read_text().splitlines() == [f"watch --socket {sock}"]


def test_it_finds_scrobd_in_local_bin_when_the_desktop_path_lacks_it(tmp_path):
    """An mpv opened from the desktop inherits a PATH without ~/.local/bin, where uv puts it."""
    home = tmp_path / "home"
    record = stub_scrobd(tmp_path, home / ".local" / "bin")
    sock = tmp_path / "mpv.sock"
    play(tmp_path, f"--input-ipc-server={sock}", CLIP, PATH=os.defpath, HOME=str(home))
    assert wait_for(record)
    assert record.read_text().splitlines() == [f"watch --socket {sock}"]


def test_a_scrobd_already_on_path_wins_over_the_one_in_local_bin(tmp_path):
    """Appended, not prepended: the `scrobd` PATH already finds is still the one that runs."""
    home = tmp_path / "home"
    in_local_bin = stub_scrobd(tmp_path, home / ".local" / "bin")
    on_path = stub_scrobd(tmp_path)
    play(tmp_path, f"--input-ipc-server={tmp_path / 'mpv.sock'}", CLIP, HOME=str(home))
    assert wait_for(on_path)
    assert not in_local_bin.exists()


def test_it_watches_the_socket_a_script_moved_it_to_not_the_one_mpv_started_with(tmp_path):
    # discord.lua moves input-ipc-server off the path mpv started with. The fixture
    # does too, at a moment that makes the outcome deterministic; see its header.
    record = stub_scrobd(tmp_path)
    first, final = tmp_path / "first.sock", tmp_path / "final.sock"
    play(
        tmp_path,
        f"--input-ipc-server={first}",
        f"--script={RESETS_SOCKET}",
        f"--script-opts=resets_ipc_server-path={final}",
        CLIP,
    )
    assert wait_for(record)
    assert record.read_text().splitlines() == [f"watch --socket {final}"]


def test_it_starts_once_however_many_files_mpv_plays(tmp_path):
    record = stub_scrobd(tmp_path)
    sock = tmp_path / "mpv.sock"
    play(tmp_path, f"--input-ipc-server={sock}", CLIP, CLIP)
    assert wait_for(record)
    time.sleep(0.5)
    assert len(record.read_text().splitlines()) == 1


def test_the_watchers_stderr_is_kept(tmp_path):
    stub_scrobd(tmp_path)
    play(tmp_path, f"--input-ipc-server={tmp_path / 'mpv.sock'}", CLIP)
    err = tmp_path / "state" / "scrobd" / "watch.stderr"
    assert wait_for(err)
    deadline = time.monotonic() + 5
    while "stub-stderr" not in err.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "stub-stderr" in err.read_text()


def test_it_does_nothing_without_an_ipc_socket(tmp_path):
    record = stub_scrobd(tmp_path)
    play(tmp_path, CLIP)
    assert not wait_for(record, 1.0)


def test_it_can_be_switched_off(tmp_path):
    record = stub_scrobd(tmp_path)
    play(
        tmp_path,
        f"--input-ipc-server={tmp_path / 'mpv.sock'}",
        "--script-opts=scrobd-enabled=no",
        CLIP,
    )
    assert not wait_for(record, 1.0)
