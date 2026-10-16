"""Shared fixtures."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

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


def _serve():
    """Run a fake push server on 127.0.0.1; yield its URL and what it saw.

    `state["mode"]` sets how it answers: `ok` acknowledges every line, `fail` is a 503,
    `short` acknowledges one line fewer than it was sent, `notjson` answers 201 with a
    body that is not JSON, and `redirect` answers 301 to `state["location"]`.
    `state["accept"]`, if set, is how many requests it acknowledges before it answers
    503 to the rest. A GET is recorded in `auth` too: urllib follows a 301 to a POST
    with a GET that still carries the Authorization header.
    """
    state: dict[str, Any] = {
        "batches": [],
        "mode": "ok",
        "auth": [],
        "accept": None,
        "location": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["auth"].append(self.headers.get("Authorization"))
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["auth"].append(self.headers.get("Authorization"))
            if state["mode"] == "redirect":
                self.send_response(301)
                self.send_header("Location", state["location"])
                self.end_headers()
                return
            if state["mode"] == "fail" or len(state["batches"]) == state["accept"]:
                self.send_response(503)
                self.end_headers()
                return
            lines = body["lines"]
            state["batches"].append(lines)
            ack = {"received": len(lines), "added": len(lines), "duplicate": 0, "rejected": 0}
            if state["mode"] == "short":
                ack["added"] -= 1
            data = json.dumps(ack).encode() if state["mode"] != "notjson" else b"<html>"
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # Silences the per-request line on stderr, which `watch` and `push` tests read.
        # Not `log_message`: its parameter is named `format`, a builtin the override
        # would have to shadow to stay a sound override.
        def log_request(self, code="-", size="-"):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # A short poll: `shutdown` waits for the next one, half a second by default, every test.
    threading.Thread(target=srv.serve_forever, args=(0.01,), daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def server():
    """Run a fake push server; see `_serve`."""
    yield from _serve()


@pytest.fixture
def other_server():
    """Run a second fake push server, for the first to redirect to."""
    yield from _serve()
