# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP layer for portal-api calls.

Wraps a ``Transport`` with:

- ``Authorization: Bearer`` injection from the ``AuthManager``
- One-shot refresh-on-401 retry
- Mapping of HTTP errors to the typed exception hierarchy in
  ``gratisgis_client.errors``

Endpoint modules use ``PortalHttp.request_json(...)`` rather than
hitting the transport directly, so the error mapping and refresh
logic stay in one place.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, Protocol
from urllib.parse import urlencode

from gratisgis_client.config import PortalConfig
from gratisgis_client.errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    PortalError,
    ValidationError,
)
from gratisgis_client.transport import (
    Transport,
    TransportError,
    TransportRequest,
    TransportResponse,
    UrllibTransport,
    encode_multipart,
)

_log = logging.getLogger(__name__)

# Regular JSON calls: generous enough for a slow county-scale list
# response, small enough that a dead portal fails within a dialog's
# patience.
_DEFAULT_TIMEOUT = 60.0

# Multipart uploads: 10 minutes covers a half-GB upload on a
# 1 MB/s link with margin. Real wall-clock would be much less on
# normal links; this is a safety net, not a target.
_MULTIPART_TIMEOUT = 600.0


class _TokenSource(Protocol):
    """The slice of ``AuthManager`` this layer needs.

    A structural protocol so tests can hand in a two-method fake
    without subclassing the real manager.
    """

    def access_token(self) -> str: ...

    def force_refresh(self) -> str: ...


class PortalHttp:
    """Thin wrapper over a ``Transport`` that injects auth and maps errors."""

    def __init__(
        self,
        config: PortalConfig,
        auth: _TokenSource,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._config = config
        self._auth = auth
        self._transport: Transport = (
            transport
            if transport is not None
            else UrllibTransport(verify_tls=config.verify_tls)
        )

    def close(self) -> None:
        """Release resources. Currently a no-op.

        Kept so ``GratisGISClient.close()`` has a stable hook if the
        transport ever grows pooled connections.
        """

    def request_json(
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
        request_headers: dict[str, str] = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        body: bytes | None = None
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        return self._send_authed(
            method,
            path,
            params=params,
            body=body,
            headers=request_headers,
            timeout=_DEFAULT_TIMEOUT,
            expect_status=expect_status,
        )

    def request_multipart(
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
        fields = {key: str(value) for key, value in (data or {}).items()}
        content_type, body = encode_multipart(fields, files or {})
        request_headers: dict[str, str] = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        # Set after the caller's headers so the boundary-bearing value
        # cannot be clobbered; a mismatched boundary breaks the parse
        # server-side in a way that reads as a corrupt upload.
        request_headers["Content-Type"] = content_type
        return self._send_authed(
            method,
            path,
            params=params,
            body=body,
            headers=request_headers,
            timeout=timeout if timeout is not None else _MULTIPART_TIMEOUT,
            expect_status=expect_status,
        )

    def _send_authed(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
        expect_status: int | None,
    ) -> Any:
        url = self._build_url(path, params)
        headers.setdefault("User-Agent", self._config.user_agent)
        headers["Authorization"] = f"Bearer {self._auth.access_token()}"

        response = self._send(method, url, headers, body, timeout, path=path)
        if response.status == 401:
            # One-shot retry: force a refresh and re-send.
            _log.debug("401 from %s %s; refreshing and retrying once", method, path)
            headers["Authorization"] = f"Bearer {self._auth.force_refresh()}"
            response = self._send(method, url, headers, body, timeout, path=path)

        return self._handle_response(response, method=method, path=path, expect_status=expect_status)

    def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
        *,
        path: str,
    ) -> TransportResponse:
        request = TransportRequest(
            method=method, url=url, headers=dict(headers), body=body, timeout=timeout
        )
        try:
            return self._transport.send(request)
        except TransportError as exc:
            # status=None distinguishes "never got an HTTP answer"
            # from every mapped portal error.
            raise PortalError(
                f"Portal {method} {path} failed before a response arrived: {exc}",
                status=None,
            ) from exc

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        base = self._config.api_base
        url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
        if params:
            pairs: list[tuple[str, str]] = []
            for key, value in params.items():
                if value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    pairs.extend((key, str(item)) for item in value)
                else:
                    pairs.append((key, str(value)))
            if pairs:
                url = f"{url}?{urlencode(pairs)}"
        return url

    def _handle_response(
        self,
        response: TransportResponse,
        *,
        method: str,
        path: str,
        expect_status: int | None,
    ) -> Any:
        status = response.status
        if expect_status is not None and status != expect_status:
            raise self._error_for(status, response, method, path)
        if 200 <= status < 300:
            if status == 204 or not response.body:
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
        status: int, response: TransportResponse, method: str, path: str
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


def _safe_json(response: TransportResponse) -> Any:
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
