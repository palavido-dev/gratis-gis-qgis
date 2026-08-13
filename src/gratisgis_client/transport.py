# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP transport seam: the one place that touches the network.

Everything above this module (``PortalHttp``, ``AuthManager``,
``discover``) speaks in ``TransportRequest`` / ``TransportResponse``
values against the ``Transport`` protocol. The default implementation,
``UrllibTransport``, is pure stdlib so the client can be vendored into
a stock QGIS install, which ships no third-party wheels. Tests inject
a fake ``Transport`` instead of mocking a real HTTP stack.

Design notes:

- Non-2xx statuses are normal responses here, not exceptions. Status
  interpretation (error mapping, refresh-on-401) belongs to
  ``PortalHttp``; keeping the transport judgment-free means the fake
  used in tests cannot diverge from the real one on that axis.
- Only genuine transport failures (DNS, refused connection, TLS,
  timeout) raise, always as ``TransportError``, so callers have a
  single exception type to map into the public error hierarchy.
"""

from __future__ import annotations

import http.client
import json
import secrets
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import IO, Any, Protocol


class TransportError(Exception):
    """A request never produced an HTTP response.

    Covers DNS failures, refused connections, TLS handshake errors,
    and timeouts. HTTP error statuses are NOT this; they come back as
    ordinary ``TransportResponse`` values.
    """


@dataclass(frozen=True)
class TransportRequest:
    """One outgoing HTTP request, fully assembled.

    ``body`` is the exact bytes to send (callers do their own JSON,
    form, or multipart encoding so the transport stays format-blind),
    or an open binary reader for bodies too large to buffer (the QGIS
    plugin streams multi-GB raster uploads from disk this way). A
    streaming body must be paired with an explicit ``Content-Length``
    header: without one urllib falls back to chunked transfer
    encoding, which S3-style presigned PUT targets reject. Streaming
    bodies also cannot survive a redirect (the reader is already
    consumed), so use them only against endpoints that answer
    directly, like a presigned object URL.
    ``timeout`` is the per-request total in seconds; urllib applies it
    as the socket timeout, so a stalled connection fails within it but
    a slowly-trickling large upload or download can legitimately
    exceed it.
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | IO[bytes] | None = None
    timeout: float = 60.0


@dataclass(frozen=True)
class TransportResponse:
    """One HTTP response, fully read into memory.

    ``url`` is the final URL after any redirects, which discovery uses
    to canonicalize the portal base. ``headers`` preserves the casing
    the server sent; use :meth:`header` for lookups because HTTP
    header names are case-insensitive on the wire.
    """

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    @property
    def text(self) -> str:
        """Body decoded as UTF-8, unmappable bytes replaced.

        Used for error surfaces (log lines, exception bodies) where a
        best-effort string beats an exception about the exception.
        """
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON. Raises ``ValueError`` when it isn't."""
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise ValueError(f"response body is not valid JSON: {exc}") from exc


class Transport(Protocol):
    """The injectable seam between the client and the network."""

    def send(self, request: TransportRequest) -> TransportResponse:
        """Perform the request, following redirects, and return the
        final response. Raises ``TransportError`` when no HTTP
        response could be obtained at all."""
        ...


class UrllibTransport:
    """Default ``Transport`` built on ``urllib.request``.

    Follows redirects via urllib's default handler chain (covering the
    www / no-www and http to https canonicalization hops common behind
    reverse proxies). ``verify_tls=False`` disables certificate and
    hostname checks; that is for local self-signed development only.
    """

    def __init__(self, *, verify_tls: bool = True) -> None:
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        # build_opener keeps the default handlers (redirects, proxy
        # from the environment) and swaps in only the HTTPS handler so
        # our ssl context applies.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context)
        )

    def send(self, request: TransportRequest) -> TransportResponse:
        req = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with self._opener.open(req, timeout=request.timeout) as raw:
                return TransportResponse(
                    status=raw.status,
                    headers=dict(raw.headers.items()),
                    body=raw.read(),
                    url=raw.geturl(),
                )
        except urllib.error.HTTPError as err:
            # urllib raises on every non-2xx/3xx; to us those are
            # ordinary responses. Read the body so PortalHttp can
            # surface the portal's structured error detail.
            try:
                body = err.read()
            except (OSError, ValueError):
                body = b""
            headers = dict(err.headers.items()) if err.headers is not None else {}
            return TransportResponse(
                status=err.code,
                headers=headers,
                body=body,
                url=err.geturl() or request.url,
            )
        except urllib.error.URLError as err:
            raise TransportError(
                f"{request.method} {request.url} failed: {err.reason}"
            ) from err
        except (OSError, http.client.HTTPException) as err:
            # OSError covers timeouts and TLS errors raised mid-read;
            # HTTPException covers protocol-level failures such as a
            # server hanging up mid-response.
            raise TransportError(
                f"{request.method} {request.url} failed: {err}"
            ) from err


def encode_multipart(
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[str, bytes]:
    """Encode a multipart/form-data body per RFC 2046 and RFC 7578.

    ``files`` maps field name to ``(filename, content, content_type)``.
    Returns ``(content_type_header_value, body)`` where the header
    value already carries the boundary parameter.

    The boundary is random so hostile file content cannot be crafted
    to contain it. The literal double-hyphen prefixes below are the
    multipart delimiter syntax RFC 2046 requires, not prose.
    """
    boundary = f"gratisgis-{secrets.token_hex(16)}"
    delimiter = ("--" + boundary).encode("ascii")
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(delimiter)
        parts.append(
            f'Content-Disposition: form-data; name="{_quote_token(name)}"'.encode()
        )
        parts.append(b"")
        parts.append(value.encode("utf-8"))
    for name, (filename, content, content_type) in files.items():
        parts.append(delimiter)
        parts.append(
            (
                f'Content-Disposition: form-data; name="{_quote_token(name)}"; '
                f'filename="{_quote_token(filename)}"'
            ).encode()
        )
        parts.append(f"Content-Type: {content_type}".encode("ascii"))
        parts.append(b"")
        parts.append(content)
    parts.append(delimiter + b"--")
    parts.append(b"")
    body = b"\r\n".join(parts)
    return f"multipart/form-data; boundary={boundary}", body


def _quote_token(value: str) -> str:
    """Sanitize a name/filename for a Content-Disposition parameter.

    Double quotes become %22 (the percent-encoding browsers emit,
    which servers decode), and CR/LF are stripped outright so a
    hostile filename cannot inject headers into the part.
    """
    return (
        value.replace('"', "%22").replace("\r", "").replace("\n", "")
    )
