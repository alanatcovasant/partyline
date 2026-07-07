"""Unit tests for `partyline watch --exit-on-mention` (the wake-up trigger).

Serves a canned SSE stream from a local HTTP server and drives the real CLI
entrypoint, asserting exit codes: 0 on addressed traffic, 2 on stream end
without a match.
"""

import http.server
import json
import threading

import pytest

from covasant_partyline import cli


class _SSEHandler(http.server.BaseHTTPRequestHandler):
    events: list[dict] = []

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: hello\ndata: {}\n\n")
        for evt in self.events:
            self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
        # stream ends -> unmatched watchers exit 2

    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture()
def sse_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _run_watch(server, agent_id):
    url = f"http://127.0.0.1:{server.server_address[1]}"
    with pytest.raises(SystemExit) as exc:
        cli.main(["--url", url, "watch", "--exit-on-mention", agent_id, "--timeout", "5"])
    return exc.value.code


def test_exits_0_on_recipient_match(sse_server, capsys):
    _SSEHandler.events = [
        {"id": 1, "agent_id": "peer", "recipient": "other", "mentions": []},
        {"id": 2, "agent_id": "peer", "recipient": "me_1", "mentions": []},
    ]
    # matching return path exits via return (SystemExit only on failure), so call directly
    url = f"http://127.0.0.1:{sse_server.server_address[1]}"
    cli.main(["--url", url, "watch", "--exit-on-mention", "me_1", "--timeout", "5"])
    out = capsys.readouterr().out
    assert '"id": 2' in out  # printed the matched event, not the earlier one
    assert '"id": 1' not in out


def test_exits_0_on_mention_match(sse_server, capsys):
    _SSEHandler.events = [
        {"id": 3, "agent_id": "peer", "recipient": None, "mentions": ["me_1", "other"]},
    ]
    url = f"http://127.0.0.1:{sse_server.server_address[1]}"
    cli.main(["--url", url, "watch", "--exit-on-mention", "me_1", "--timeout", "5"])
    assert '"id": 3' in capsys.readouterr().out


def test_exits_2_when_stream_ends_without_match(sse_server):
    _SSEHandler.events = [
        {"id": 4, "agent_id": "peer", "recipient": "someone_else", "mentions": ["not_me"]},
    ]
    assert _run_watch(sse_server, "me_1") == 2


def test_ignores_malformed_event_data(sse_server):
    class _BadHandler(_SSEHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: not-json\n\n")

    sse_server.RequestHandlerClass = _BadHandler
    assert _run_watch(sse_server, "me_1") == 2
