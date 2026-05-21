# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP transport for portal-api calls.

Wraps ``httpx.AsyncClient`` with:

- ``Authorization: Bearer`` injection from the ``AuthManager``
- One-shot refresh-on-401 retry
- Mapping of HTTP errors to the typed exception hierarchy in
  ``gratisgis_client.errors``

Endpoint modules use ``PortalHttp.request_json(...)`` rather than
hitting ``httpx`` directly, so the error mapping and refresh logic
stay in one place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from gratisgis_client.errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    PortalError,
    ValidationError,
)

if TYPE_CHECKING:
    from gratisgis_client.auth.manager import AuthManager
    from gratisgis_client.config import PortalConfig

_log = logging.getLogger(__name__)


class PortalHttp:
    """Thin wrapper over httpx that injects auth and maps errors."""

    def __init__(
        self,
        config: PortalConfig,
        auth: AuthManager,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._auth = auth
        self._client = client
        self._owns_client = client is None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.api_base,
                verify=self._config.verify_tls,
                headers={"User-Agent": self._config.user_agent},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        expect_status: int | None = None,
    ) -> Any:
        """Perform a portal-api call and decode the JSON response.

        ``path`` is relative to ``config.api_base``. ``expect_status``
        is optional; if set, a different status raises ``PortalError``
        even if it would otherwise be a 2xx.

        Returns the parsed JSON body. Endpoint modules turn it into a
        typed model.
        """
        return await self._request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            expect_status=expect_status,
            stream=False,
        )

    async def request_multipart(
        self,
        method: str,
        path: str,
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        expect_status: int | None = None,
    ) -> Any:
        """POST a multipart/form-data payload (file upload).

        ``files`` maps field-name -> (filename, bytes, content_type).
        Used by the ingest endpoint, which buffers the whole file into
        memory before sending; that's fine for the county-scale data
        the portal targets (under 1 GB) and keeps the call signature
        simple.

        Uses a longer default timeout than ``request_json`` because
        a 500 MB GeoPackage upload over a slow link routinely exceeds
        the 60 s default. Callers can override ``timeout`` if they
        already know their file size and link speed.
        """
        client = await self._ensure_client()
        access = await self._auth.access_token()
        request_headers: dict[str, str] = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        request_headers["Authorization"] = f"Bearer {access}"
        # httpx infers content-type from the files= kwarg; passing it
        # in headers would override that and break the boundary.

        # 10 minutes covers a half-GB upload on a 1 MB/s link with
        # margin. Real wall-clock would be much less on normal links;
        # this is a safety net, not a target.
        effective_timeout = timeout if timeout is not None else 600.0

        response = await client.request(
            method,
            path,
            params=params,
            files=files,
            data=data,
            headers=request_headers,
            timeout=httpx.Timeout(effective_timeout, connect=10.0),
        )
        if response.status_code == 401:
            access = await self._auth.force_refresh()
            request_headers["Authorization"] = f"Bearer {access}"
            response = await client.request(
                method,
                path,
                params=params,
                files=files,
                data=data,
                headers=request_headers,
                timeout=httpx.Timeout(effective_timeout, connect=10.0),
            )

        return self._handle_response(
            response, method=method, path=path, expect_status=expect_status
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: Any | None,
        headers: dict[str, str] | None,
        expect_status: int | None,
        stream: bool,
    ) -> Any:
        client = await self._ensure_client()
        access = await self._auth.access_token()
        request_headers: dict[str, str] = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        request_headers["Authorization"] = f"Bearer {access}"

        response = await client.request(
            method, path, params=params, json=json, headers=request_headers
        )
        if response.status_code == 401:
            # One-shot retry: force a refresh and re-send.
            _log.debug("401 from %s %s; refreshing and retrying once", method, path)
            access = await self._auth.force_refresh()
            request_headers["Authorization"] = f"Bearer {access}"
            response = await client.request(
                method, path, params=params, json=json, headers=request_headers
            )

        return self._handle_response(response, method=method, path=path, expect_status=expect_status)

    def _handle_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        expect_status: int | None,
    ) -> Any:
        status = response.status_code
        if expect_status is not None and status != expect_status:
            raise self._error_for(status, response, method, path)
        if 200 <= status < 300:
            if status == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise PortalError(
                    f"Portal returned non-JSON {method} {path}: {exc}",
                    status=status,
                    body=response.text,
                ) from exc
        raise self._error_for(status, response, method, path)

    @staticmethod
    def _error_for(
        status: int, response: httpx.Response, method: str, path: str
    ) -> PortalError:
        body = _safe_json(response)
        code = _extract_code(body)
        message = f"Portal {method} {path} failed: HTTP {status}"
        if status in (401, 403):
            return AuthError(message, status=status, body=body, code=code)
        if status == 404:
            return NotFoundError(message, status=status, body=body, code=code)
        if status == 409:
            return ConflictError(message, status=status, body=body, code=code)
        if 400 <= status < 500:
            return ValidationError(message, status=status, body=body, code=code)
        return PortalError(message, status=status, body=body, code=code)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _extract_code(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("code", "errorCode", "error"):
            value = body.get(key)
            if isinstance(value, str):
                return value
    return None
