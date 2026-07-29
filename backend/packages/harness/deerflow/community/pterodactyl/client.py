"""Async HTTP client for the Pterodactyl Client API.

Centralizes auth headers, timeouts, retry on transient failures, and error
normalization so the tool layer only deals with parsed JSON or typed errors.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import PterodactylConfig, load_config
from .errors import (
    PterodactylAPIError,
    PterodactylAuthError,
    PterodactylNotFoundError,
    PterodactylTimeoutError,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2


class PterodactylClient:
    """Thin async wrapper around the Pterodactyl Client API."""

    def __init__(self, config: PterodactylConfig | None = None) -> None:
        self._config = config or load_config()

    @property
    def config(self) -> PterodactylConfig:
        return self._config

    def _headers(self, *, raw_body: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Accept": "application/json",
            "Content-Type": "text/plain" if raw_body else "application/json",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        content: str | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Perform an authenticated request against the Client API.

        Args:
            method: HTTP method (GET/POST/PUT/DELETE).
            path: Path relative to the client API root, e.g. ``/servers``.
            params: Optional query params.
            json: Optional JSON body.
            content: Optional raw text body (e.g. file contents for writes);
                mutually exclusive with ``json`` and sent as text/plain.
            expect_json: When False, return raw text (used for file reads).

        Returns:
            Parsed JSON (dict) or raw text, or None for empty (204) responses.

        Raises:
            PterodactylAuthError / PterodactylNotFoundError / PterodactylAPIError /
            PterodactylTimeoutError on failure.
        """
        url = f"{self._config.base_url}{path}"
        headers = self._headers(raw_body=content is not None)
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                    response = await client.request(method, url, params=params, json=json, content=content, headers=headers)
                if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return self._handle_response(response, expect_json=expect_json)
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise PterodactylTimeoutError(f"Request to {path} timed out") from exc
            except httpx.HTTPError as exc:
                raise PterodactylAPIError(f"HTTP error calling {path}: {exc}") from exc

        # Exhausted retries on retryable status codes.
        raise PterodactylTimeoutError(f"Request to {path} failed after retries: {last_exc}")

    async def download(self, path: str, dest, *, params: dict[str, Any] | None = None) -> int:
        """Stream a Client API download to ``dest`` (a binary file object).

        Two-step Pterodactyl flow: ``path`` (e.g. ``/servers/{id}/files/download``)
        returns a signed one-time URL, which is then fetched without auth headers.
        Bytes are streamed to disk so large/binary files never enter memory whole.
        Returns the number of bytes written.
        """
        meta = await self.request("GET", path, params=params)
        url = ((meta or {}).get("attributes") or {}).get("url")
        if not url:
            raise PterodactylAPIError(f"Panel did not return a download URL for {path}")

        written = 0
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise PterodactylAPIError(f"Download failed with {response.status_code}", status_code=response.status_code)
                    async for chunk in response.aiter_bytes():
                        dest.write(chunk)
                        written += len(chunk)
        except httpx.TimeoutException as exc:
            raise PterodactylTimeoutError(f"Download of {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise PterodactylAPIError(f"HTTP error downloading {path}: {exc}") from exc
        return written

    def _handle_response(self, response: httpx.Response, *, expect_json: bool) -> Any:
        status = response.status_code
        if status == 401 or status == 403:
            raise PterodactylAuthError(
                "Authentication failed (check the Client API key and its permissions)",
                status_code=status,
            )
        if status == 404:
            raise PterodactylNotFoundError("Resource not found", status_code=status)
        if status >= 400:
            raise PterodactylAPIError(
                f"Pterodactyl API returned {status}",
                status_code=status,
                detail=_extract_detail(response),
            )
        if status == 204 or not response.content:
            return None
        if not expect_json:
            return response.text
        return response.json()


def _extract_detail(response: httpx.Response) -> str | None:
    """Best-effort extraction of the first error detail from a panel error body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or None
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return first.get("detail") or first.get("code")
    return None
