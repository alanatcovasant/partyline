"""Unit test for the /mcp trailing-slash middleware.

A bare POST /mcp must be rewritten to /mcp/ before routing (the Starlette mount
only serves the slashed path, and a 307 wouldn't replay the POST body) — the
bug that broke every MCP client against the deployed app.
"""

import pytest

from app.main import _mcp_trailing_slash


class FakeRequest:
    def __init__(self, path):
        self.scope = {"path": path}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/mcp", "/mcp/"),          # the fix
        ("/mcp/", "/mcp/"),         # already correct
        ("/api/messages", "/api/messages"),  # untouched
        ("/", "/"),
        ("/mcpx", "/mcpx"),         # prefix lookalike untouched
    ],
)
async def test_path_rewrite(path, expected):
    req = FakeRequest(path)
    seen = {}

    async def call_next(request):
        seen["path"] = request.scope["path"]
        return "response"

    result = await _mcp_trailing_slash(req, call_next)
    assert result == "response"
    assert seen["path"] == expected
