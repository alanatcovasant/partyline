"""Local MCP bridge (PRD FR11 / §10) — stdio <-> HTTP proxy to the Partyline App.

Databricks platform auth (OAuth2 M2M, service principals, token refresh) is awkward for an
agent to handle inline. This bridge runs as a subprocess spawned by the MCP client: it
speaks MCP stdio to the agent and transparently proxies every request to the remote App's
`/mcp` (Streamable HTTP) endpoint, injecting a Databricks bearer token resolved by the
`databricks-sdk` ``Config`` (env vars, CLI profile, or service principal credentials).

The agent's only setup is a `.mcp.json` entry — no Databricks SDK knowledge required:

    {
      "mcpServers": {
        "partyline": {
          "command": "python",
          "args": ["-m", "covasant_partyline.bridge", "--app-name", "covasant-partyline"],
          "env": {"DATABRICKS_CLIENT_ID": "...", "DATABRICKS_CLIENT_SECRET": "...",
                  "DATABRICKS_HOST": "https://<workspace>.databricks.net"}
        }
      }
    }

Pass either --url (full https URL of the app's /mcp endpoint) or --app-name (resolved to
the app URL via the SDK).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from databricks.sdk.core import Config

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def _auth_headers(cfg: Config) -> dict[str, str]:
    """Resolve Databricks OAuth auth headers (Authorization: Bearer ...).

    Databricks Apps do **not** accept personal access tokens — they authenticate callers
    via OAuth. So we require an OAuth strategy (M2M service principal or U2M) and refuse a
    PAT outright, rather than sending a token the app will reject with 401.
    """
    auth_type = (getattr(cfg, "auth_type", "") or "").lower()
    if auth_type == "pat" or cfg.token:
        raise SystemExit(
            "Databricks Apps do not accept PATs. Use OAuth: set DATABRICKS_CLIENT_ID + "
            "DATABRICKS_CLIENT_SECRET (M2M) or run `databricks auth login` (U2M). "
            "Unset DATABRICKS_TOKEN."
        )
    headers = cfg.authenticate()  # dict of headers for the current credential strategy
    if "Authorization" not in headers:
        raise SystemExit(
            "Could not resolve Databricks OAuth credentials. Set DATABRICKS_HOST and "
            "DATABRICKS_CLIENT_ID/SECRET, or authenticate a U2M OAuth profile."
        )
    return dict(headers)


def _resolve_mcp_url(cfg: Config, url: str | None, app_name: str | None) -> str:
    if url:
        return url
    if not app_name:
        raise SystemExit("Provide --url or --app-name.")
    from databricks.sdk import WorkspaceClient

    app = WorkspaceClient(config=cfg).apps.get(name=app_name)
    if not app.url:
        raise SystemExit(f"App '{app_name}' has no URL yet (is it deployed and running?).")
    return app.url.rstrip("/") + "/mcp"


async def _serve(mcp_url: str, headers: dict[str, str]) -> None:
    async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as remote:
            await remote.initialize()
            proxy = Server("covasant-partyline-bridge")

            @proxy.list_tools()
            async def list_tools():
                return (await remote.list_tools()).tools

            @proxy.call_tool()
            async def call_tool(name: str, arguments: dict):
                result = await remote.call_tool(name, arguments)
                # Forward content plus any structured payload the tool returned.
                return result.content, getattr(result, "structuredContent", None)

            opts = proxy.create_initialization_options()
            async with stdio_server() as (r, w):
                await proxy.run(r, w, opts)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="covasant_partyline.bridge", description=__doc__.splitlines()[0])
    p.add_argument("--url", help="Full https URL of the app's /mcp endpoint")
    p.add_argument("--app-name", help="Databricks App name (resolved to URL via SDK)")
    p.add_argument("--host", help="Databricks workspace host (overrides DATABRICKS_HOST)")
    args = p.parse_args(argv)

    cfg = Config(host=args.host) if args.host else Config()
    headers = _auth_headers(cfg)
    mcp_url = _resolve_mcp_url(cfg, args.url, args.app_name)
    print(f"partyline bridge -> {mcp_url}", file=sys.stderr)
    asyncio.run(_serve(mcp_url, headers))


if __name__ == "__main__":
    main()
