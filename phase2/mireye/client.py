"""Async client for the Mireye Earth API.

Rules baked in here rather than left to callers:

* Timeouts are generous by default. A too-short client timeout does NOT cancel
  server-side work -- it keeps running and billing.
* ``retryable`` is honoured, not the HTTP status code. Retryable failures carry
  ``Retry-After``.
* We send our own ``X-Request-ID`` and keep it. Unhandled 500s carry no body and no
  header, so the id we sent is the only way to correlate with Mireye's logs.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx

from ..config import Settings, get_settings
from .schemas import FetchResponse, GeocodeResponse, MireyeError, QuoteResponse


def _new_request_id() -> str:
    return f"p2-{uuid.uuid4().hex[:16]}"


class MireyeClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> "MireyeClient":
        self._client = httpx.AsyncClient(
            base_url=self.settings.mireye_base_url,
            transport=self._transport,
            timeout=httpx.Timeout(self.settings.phase2_fetch_timeout_s),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("MireyeClient used outside its async context manager")
        return self._client

    # -- plumbing -----------------------------------------------------------
    def _headers(self, request_id: str, *, authed: bool = True) -> dict[str, str]:
        headers = {"content-type": "application/json", "X-Request-ID": request_id}
        if authed:
            token = self.settings.mireye_api_token
            if not token:
                raise MireyeError(
                    "auth_missing",
                    "MIREYE_API_TOKEN is not set",
                    retryable=False,
                )
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _raise_for_error(response: httpx.Response, request_id: str) -> None:
        if response.status_code < 400:
            return
        code, message, retryable = "http_error", response.text[:500], False
        try:
            detail = response.json().get("detail") or {}
            if isinstance(detail, dict):
                code = detail.get("error", code)
                message = detail.get("message", message)
                retryable = bool(detail.get("retryable", False))
        except Exception:  # noqa: BLE001 -- unhandled 500s have no structured body
            pass
        raise MireyeError(
            code,
            message,
            retryable=retryable,
            status_code=response.status_code,
            request_id=response.headers.get("X-Request-ID", request_id),
            retry_after=response.headers.get("Retry-After"),
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        request_id = _new_request_id()
        headers = self._headers(request_id)
        if extra_headers:
            headers.update(extra_headers)
        response = await self.client.post(path, json=payload, headers=headers, timeout=timeout)
        self._raise_for_error(response, request_id)
        return response.json(), response.headers.get("X-Request-ID", request_id)

    # -- endpoints ----------------------------------------------------------
    async def quote(
        self,
        *,
        fields: list[str] | None = None,
        preset: str | None = None,
        locations: int = 1,
    ) -> QuoteResponse:
        """Free, unmetered, exact. Computed by the same code that charges, so it cannot
        drift from the bill. Prices the request SHAPE -- no coordinates involved."""
        payload: dict[str, Any] = {"locations": locations}
        if fields:
            payload["fields"] = fields
        if preset:
            payload["preset"] = preset
        data, _ = await self._post(
            "/v1/fetch/quote", payload, timeout=self.settings.phase2_meta_timeout_s
        )
        return QuoteResponse.model_validate(data)

    async def fetch(
        self,
        *,
        lat: float | None = None,
        lng: float | None = None,
        address: str | None = None,
        fields: list[str],
        idempotency_key: str | None = None,
    ) -> tuple[FetchResponse, str]:
        """Named fields at ONE location. Returns the parsed response and the request id."""
        if (lat is None or lng is None) and not address:
            raise MireyeError("invalid_locator", "need lat+lng or address", retryable=False)
        if address and (lat is not None or lng is not None):
            raise MireyeError(
                "invalid_locator", "send lat+lng or address, never both", retryable=False
            )

        payload: dict[str, Any] = {"fields": fields}
        if address:
            payload["address"] = address
            timeout = self.settings.phase2_fetch_address_timeout_s
        else:
            payload["lat"], payload["lng"] = lat, lng
            timeout = self.settings.phase2_fetch_timeout_s

        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        data, request_id = await self._post(
            "/v1/fetch", payload, timeout=timeout, extra_headers=headers
        )
        return FetchResponse.model_validate(data), request_id

    async def fetch_batch(
        self,
        locations: list,
        fields: list,
        *,
        idempotency_key: str | None = None,
    ) -> list:
        """<=25 locations, ONE batch-wide field selection, one call.

        Three properties from the reference this relies on:
          1. ``results[i]`` answers ``locations[i]`` and carries ``index`` explicitly.
          2. Each ``ok: true`` entry IS a ``/v1/fetch`` response body -- so
             ``FetchResponse`` parses it with no new code.
          3. A location's failure is an *entry* (``ok: false``), never an HTTP failure.
             Location 7's bad address cannot cost you the other 24 results.

        Returns ``[(index, FetchResponse | None, error_dict | None)]``. Entry-level
        failure (the location itself) and field-level failure (``partial_failures``
        inside a good entry) are deliberately NOT conflated -- the caller sees both.
        """
        if not locations:
            return []
        if len(locations) > 25:
            raise MireyeError(
                "invalid_request", f"{len(locations)} locations exceeds the batch cap of 25",
                retryable=False,
            )

        payload: dict[str, Any] = {"locations": locations, "fields": fields}
        headers = {"Idempotency-Key": idempotency_key or _new_request_id().replace("p2-", "b-")}
        data, _ = await self._post(
            "/v1/fetch/batch",
            payload,
            # A batch is 25 requests' worth of work, processed 4 at a time; worst case
            # ~90 s. A short timeout does not cancel it -- it keeps running and billing.
            timeout=max(self.settings.phase2_batch_timeout_s, 120.0),
            extra_headers=headers,
        )

        out: list = []
        for i, entry in enumerate(data.get("results") or data.get("locations") or []):
            index = entry.get("index", i)
            if entry.get("ok") is False:
                out.append((index, None, entry.get("error") or {"error": "unknown"}))
                continue
            body = entry.get("result", entry)
            out.append((index, FetchResponse.model_validate(body), None))
        return out

    async def geocode(self, address: str) -> GeocodeResponse:
        data, _ = await self._post(
            "/v1/geocode", {"address": address}, timeout=self.settings.phase2_geocode_timeout_s
        )
        return GeocodeResponse.model_validate(data)

    async def proximity_nearest(
        self,
        curated_set: str,
        lat: float,
        lng: float,
        *,
        n: int = 5,
        filters: dict | None = None,
    ) -> dict:
        """Authoritative nearest facility over a FIXED 160 km search radius.

        This is the endpoint that answers definitively what a point field could not:
        `nearest_transmission_line_voltage_kv` returned `absent` at West Seattle because
        the source's own search radius did not reach, not because no line exists.

        Straightline mode is free above the 2-credit floor, and nothing qualifying
        returns empty `candidates` rather than an error.

        NOTE: `applied_filters` echoes exactly what you SENT, not the default that ran --
        `@substations` applies a 115 kV floor whether or not you asked, and unrated
        substations stay excluded regardless. Pass `filters` explicitly if you need the
        response to state the threshold.
        """
        req: dict[str, Any] = {
            "op": "nearest",
            "set": curated_set if curated_set.startswith("@") else f"@{curated_set}",
            # Locators are STRINGS -- a coordinate or a street address, never a place
            # name. Coordinates skip the accuracy gate, cost no geocoding credit, and
            # cannot drift, so prefer them for anything resolved repeatedly.
            "origin": f"{lat},{lng}",
            "n": n,
            "mode": "straightline",
        }
        if filters:
            req["filters"] = filters
        data, _ = await self._post(
            "/v1/proximity", req, timeout=self.settings.phase2_fetch_timeout_s
        )
        return data

    async def ask(
        self,
        question: str,
        *,
        lat: float | None = None,
        lng: float | None = None,
        address: str | None = None,
        include_trace: bool = True,
    ) -> dict:
        """The LLM path -- planner, deterministic fetch, synthesizer, citation extraction.

        Used as a CROSS-CHECK, never as a scoring input: the score must stay a pure
        function of fetched values so it is reproducible and so calibration works.

        Two things to read from the response:
          * ``data_gaps`` is the authoritative missing-field array, computed from the
            fetch result rather than from the prose -- read it instead of diffing
            ``trace.fields_requested`` against ``fields_used``.
          * ``confidence`` auto-downgrades one bucket when >30% of planner-selected
            fields came back null, regardless of what the synthesizer self-reported.

        The planner caps at 15 fields, so ask narrow questions rather than
        "assess this site".
        """
        payload: dict[str, Any] = {"question": question, "include_trace": include_trace}
        if address:
            payload["address"] = address
        else:
            payload["lat"], payload["lng"] = lat, lng
        # Server hard bound is 110 s; a shorter client timeout aborts otherwise-good
        # requests AND leaves them running and billing server-side.
        data, _ = await self._post(
            "/v1/ask", payload, timeout=max(self.settings.phase2_ask_timeout_s, 120.0)
        )
        return data

    async def lookup(self, text: str) -> dict:
        """Messy locator -> canonical join keys, parcel, and free context.

        `disposition` must be checked FIRST: `clarify` means present candidates and
        never auto-pick; `no_match` means read reason/hint and ask.
        """
        data, _ = await self._post(
            "/v1/lookup", {"input": text}, timeout=self.settings.phase2_geocode_timeout_s
        )
        return data

    async def submit_run(self, locations: list, fields: list) -> dict:
        """Async fetch_batch. Returns 202 + run_id immediately.

        Caller mistakes fail at SUBMIT, not in the background, so a run never exists
        only to fail on something validation could have caught.
        """
        data, _ = await self._post(
            "/v1/runs",
            {"kind": "fetch_batch", "request": {"locations": locations, "fields": fields}},
            timeout=self.settings.phase2_meta_timeout_s,
        )
        return data

    async def get_run(self, run_id: str) -> dict:
        """Poll. `status`: queued -> running -> done | failed. The source of truth."""
        request_id = _new_request_id()
        response = await self.client.get(
            f"/v1/runs/{run_id}", headers=self._headers(request_id),
            timeout=self.settings.phase2_meta_timeout_s,
        )
        self._raise_for_error(response, request_id)
        return response.json()

    async def run_artifact(self, run_id: str, kind: str = "csv") -> str:
        """Rendered on read from the stored result, never stored separately.

        The CSV is one row per location, index-aligned, identity columns plus one
        column per field value, plus a `failed_fields` column naming what to distrust --
        which is exactly the shape weight calibration needs.
        """
        request_id = _new_request_id()
        response = await self.client.get(
            f"/v1/runs/{run_id}/artifacts/{kind}", headers=self._headers(request_id),
            timeout=self.settings.phase2_batch_timeout_s,
        )
        self._raise_for_error(response, request_id)
        return response.text

    async def meta_fields(self, etag: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
        """Public endpoint -- no token needed. Returns (payload, etag); payload is None
        on a 304 so the caller keeps its cached copy."""
        request_id = _new_request_id()
        headers = self._headers(request_id, authed=False)
        if etag:
            headers["If-None-Match"] = etag
        response = await self.client.get(
            "/v1/meta/fields", headers=headers, timeout=self.settings.phase2_meta_timeout_s
        )
        if response.status_code == 304:
            return None, etag
        self._raise_for_error(response, request_id)
        return response.json(), response.headers.get("ETag")

    async def usage(self) -> dict[str, Any]:
        request_id = _new_request_id()
        response = await self.client.get(
            "/v1/users/me/usage",
            headers=self._headers(request_id),
            timeout=self.settings.phase2_meta_timeout_s,
        )
        self._raise_for_error(response, request_id)
        return response.json()
