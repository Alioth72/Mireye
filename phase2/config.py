"""Phase 2 settings.

All config is namespaced (``MIREYE_*`` / ``PHASE2_*``) so that merging into Phase 3's
app cannot collide with their environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Mireye API ---------------------------------------------------------
    mireye_api_token: str = ""
    mireye_base_url: str = "https://api.mireye.com"

    # Timeouts. The reference is explicit that a too-short client timeout does NOT
    # cancel server-side work -- it keeps running and billing. These are floors.
    phase2_fetch_timeout_s: float = 30.0
    phase2_fetch_address_timeout_s: float = 35.5  # +5.5s for the server-side geocode leg
    phase2_geocode_timeout_s: float = 10.0
    phase2_meta_timeout_s: float = 10.0

    # --- Storage ------------------------------------------------------------
    phase2_database_url: str = "sqlite:///./phase2.db"

    # --- Behaviour ----------------------------------------------------------
    # GET auto-fetches on a cache miss (team decision: credits are not scarce).
    # Quote-first and the fetch log stay on regardless -- they are free and they are
    # the evidence Build Brief II grades.
    phase2_autofetch_on_miss: bool = True
    phase2_quote_before_fetch: bool = True

    # Catalog mirror TTL (public endpoint, ETag-cached).
    phase2_catalog_ttl_s: int = 3600

    # Runaway-loop guardrails. Not budget gates -- see ideation sec. 11.2.
    phase2_failed_negative_cache_s: int = 300
    phase2_fetch_warn_per_site_per_hour: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
