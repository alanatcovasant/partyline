"""Run the app with uvicorn, binding to the port Databricks Apps provides.

Using a module entrypoint (``python -m app``) rather than a bare ``uvicorn`` command
lets us read the injected port from the environment without shell interpolation in
app.yaml.
"""

from __future__ import annotations

import uvicorn

from .config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
