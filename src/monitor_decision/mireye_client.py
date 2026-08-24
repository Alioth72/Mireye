from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://mireye-earth.fly.dev"


class MireyeClient:
    """Small optional client for the fetch endpoint.

    The decision layer does not require live Mireye calls. Use this only after
    the watcher has detected a scoped material event, so credits are spent on
    event-driven scoring rather than polling.
    """

    def __init__(self, api_key: str | None = None, base_url: str = DEFAULT_BASE_URL, timeout_s: int = 15):
        self.api_key = api_key or os.getenv("MIREYE_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def fetch(
        self,
        *,
        lat: float,
        lng: float,
        preset: str | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"lat": lat, "lng": lng}
        if preset:
            body["preset"] = preset
        if fields:
            body["fields"] = fields

        request = urllib.request.Request(
            f"{self.base_url}/v1/fetch",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers
