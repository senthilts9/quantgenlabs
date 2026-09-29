"""Where this application is allowed to write.

Every persistent store previously hardcoded "/app/data" — the path inside the
container — as its default. Outside Docker that directory does not exist and is
not writable, so the OMS blotter returned 500 on every local run and the market
-data refreshers logged an OSError on every tick. One resolver, used everywhere,
so the three stores cannot disagree about where the data lives.

Resolution order, most specific first:
  1. An explicit environment variable, if the caller set one.
  2. QGL_DATA_DIR, for deployments that want one directory for everything.
  3. /app/data when it genuinely exists and is writable — so container behaviour
     is unchanged.
  4. <repo>/backend/data, resolved from this file rather than the working
     directory, so it does not depend on where the server was started.
  5. The user cache directory. A read-only checkout should degrade to a working
     app, not a 500 on every route that touches storage.
"""
from __future__ import annotations

import os
from pathlib import Path

_CONTAINER = Path("/app/data")


def _writable(p: Path) -> bool:
    return p.is_dir() and os.access(p, os.W_OK)


def data_dir() -> Path:
    """The directory for databases and caches. Created if it does not exist."""
    explicit = os.getenv("QGL_DATA_DIR")
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d

    if _writable(_CONTAINER):
        return _CONTAINER

    # app/core/paths.py -> app/core -> app -> backend
    local = Path(__file__).resolve().parents[2] / "data"
    try:
        local.mkdir(parents=True, exist_ok=True)
        return local
    except OSError:
        cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "quantgenlabs"
        cache.mkdir(parents=True, exist_ok=True)
        return cache


def db_path(filename: str, env_var: str | None = None) -> str:
    """Path for one database file, honouring an explicit override first."""
    if env_var:
        override = os.getenv(env_var)
        if override:
            return override
    return str(data_dir() / filename)
