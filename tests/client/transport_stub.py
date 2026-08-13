# SPDX-License-Identifier: AGPL-3.0-or-later
"""FakeTransport: the test-side implementation of the Transport seam.

Every client test drives the code through this instead of a mocked
HTTP library, so the tests exercise exactly the surface the real
``UrllibTransport`` implements and nothing more. Supports two styles:

- a queue of canned responses (``add`` / ``add_exception``), popped
  in order, for request-by-request scripting
- a ``handler`` callable, for flows where the request sequence is
  data-dependent (auth refresh hitting discovery then token
  endpoints)

Sent requests are recorded on ``requests`` for assertions.
"""

from __future__ import annotations

import json as _json
import threading
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from gratisgis_client.transport import TransportRequest, TransportResponse

Handler = Callable[[TransportRequest], TransportResponse]


def json_response(
    payload: object,
    *,
    status: int = 200,
    url: str = "",
    headers: dict[str, str] | None = None,
) -> TransportResponse:
    """A canned JSON response, the common case."""
    base = {"Content-Type": "application/json"}
    if headers:
        base.update(headers)
    return TransportResponse(
        status=status,
        headers=base,
        body=_json.dumps(payload).encode("utf-8"),
        url=url,
    )


def text_response(
    text: str,
    *,
    status: int = 200,
    url: str = "",
    content_type: str = "text/plain",
) -> TransportResponse:
    return TransportResponse(
        status=status,
        headers={"Content-Type": content_type},
        body=text.encode("utf-8"),
        url=url,
    )


class FakeTransport:
    """Implements the ``Transport`` protocol for tests.

    Thread-safe because the auth-manager tests hammer it from a
    thread pool to prove the refresh lock dedupes.
    """

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[TransportRequest] = []
        self._plan: list[TransportResponse | Exception | Handler] = []
        self._handler = handler
        self._lock = threading.Lock()

    def add(self, response: TransportResponse) -> FakeTransport:
        self._plan.append(response)
        return self

    def add_exception(self, exc: Exception) -> FakeTransport:
        self._plan.append(exc)
        return self

    def add_handler(self, handler: Handler) -> FakeTransport:
        self._plan.append(handler)
        return self

    def send(self, request: TransportRequest) -> TransportResponse:
        with self._lock:
            self.requests.append(request)
            item: TransportResponse | Exception | Handler | None
            item = self._plan.pop(0) if self._plan else self._handler
        if item is None:
            raise AssertionError(
                f"FakeTransport got an unplanned request: {request.method} {request.url}"
            )
        if isinstance(item, Exception):
            raise item
        if isinstance(item, TransportResponse):
            # Default the final URL to the requested one, mirroring a
            # redirect-free real transport, so discovery's
            # canonicalization sees a realistic value.
            if not item.url:
                return TransportResponse(
                    status=item.status,
                    headers=item.headers,
                    body=item.body,
                    url=request.url,
                )
            return item
        return item(request)


def query_of(request: TransportRequest) -> dict[str, list[str]]:
    """The request's query string, parsed."""
    return parse_qs(urlsplit(request.url).query, keep_blank_values=True)


def path_of(request: TransportRequest) -> str:
    """The request's URL path."""
    return urlsplit(request.url).path


def body_json(request: TransportRequest) -> object:
    """The request body parsed as JSON."""
    # Streaming (file-object) bodies exist only for the presigned
    # PUT path, which never goes through PortalHttp; everything the
    # fake sees is fully-assembled bytes.
    assert isinstance(request.body, bytes), "request has no in-memory body"
    return _json.loads(request.body)
