"""Run the app with uvicorn, binding to the port Databricks Apps provides.

Using a module entrypoint (``python -m app``) rather than a bare ``uvicorn`` command
lets us read the injected port from the environment without shell interpolation in
app.yaml.
"""

from __future__ import annotations

import sys

import uvicorn

from .config import settings

if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg's async connections (the LISTEN/NOTIFY hub) can't run on Windows'
        # default ProactorEventLoop, and uvicorn.run ignores the ambient loop policy —
        # so drive the server on an explicit selector loop. Local-dev only; deployed
        # apps run on Linux and take the plain uvicorn.run path below.
        import asyncio
        import selectors

        config = uvicorn.Config(
            "app.main:app", host=settings.host, port=settings.port, log_level="info"
        )
        asyncio.run(
            uvicorn.Server(config).serve(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            log_level="info",
        )
