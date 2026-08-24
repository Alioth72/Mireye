"""Console configuration.

Everything the frontend server needs comes from the environment (or a local
``.env`` file that is never committed).  Nothing here is baked into the static
bundle: ``app.py`` hands the browser only what ``/api/config`` chooses to
expose, at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The .env sits next to this file, so the server can be started from any cwd.
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    """Frontend settings.

    Names are namespaced rather than prefixed: the env var is simply the
    upper-cased field name (``BACKEND_BASE_URL``, ``GOOGLE_MAPS_BROWSER_KEY``,
    ...).  Nothing is required — an unset Maps key is a supported state, the
    console falls back to an SVG vicinity diagram.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Where the Phase 1/2/3 API lives.  The browser never sees this host; every
    # call is proxied through this server, which is why there is no CORS story.
    backend_base_url: str = "http://127.0.0.1:8000"

    # Google Maps JS API browser key.  Public by nature (it ships to the page),
    # so it MUST be restricted by HTTP referrer in the Google Cloud console.
    # Empty means "no map" — the console draws its SVG fallback instead.
    google_maps_browser_key: str = ""
    google_maps_map_id_light: str = ""
    google_maps_map_id_dark: str = ""

    # Where this server binds.
    frontend_host: str = "127.0.0.1"
    frontend_port: int = 8080

    # Upstream timeout for the proxy.
    #
    # Deliberately generous.  A bundle read on the backend can trigger a live
    # Mireye fetch, which is billed and can take tens of seconds.  Cutting the
    # client timeout short does NOT cancel the server-side work — the backend
    # keeps fetching and keeps billing, we just stop listening and lose the
    # result we already paid for.  So we wait.
    proxy_timeout_s: float = 40.0


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (cached; clear the cache in tests)."""
    return Settings()
