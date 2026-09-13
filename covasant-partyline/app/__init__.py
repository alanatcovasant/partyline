"""Covasant Partyline app package."""

# deploy.sh generates __version__.py with build metadata at deploy time.
# Locally (no deploy), fall back to dev defaults.
try:
    from .__version__ import (  # type: ignore[import-not-found]
        __build_host__,
        __build_timestamp__,
        __git_commit__,
        __version__,
    )
except ImportError:
    __version__ = "0.1.0.dev0"
    __build_timestamp__ = "dev"
    __git_commit__ = "unknown"
    __build_host__ = "local"
