# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PortalHttp: auth injection, 401 retry, error mapping."""

from __future__ import annotations

import time

import pytest

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.auth.storage import InMemoryTokenStorage
from gratisgis_client.auth.tokens import TokenSet
from gratisgis_client.config import PortalConfig
from gratisgis_client.errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    PortalError,
    ValidationError,
)
from gratisgis_client.http import PortalHttp
from gratisgis_client.transport import TransportError, TransportRequest, TransportResponse
from tests.client.transport_stub import FakeTransport, json_response, text_response


def _config() -> PortalConfig:
    return PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
    )


def _fresh() -> TokenSet:
    return TokenSet(
        access_token="access-old",
        refresh_token="refresh-1",
        access_expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 7200,
    )


def _portal(transport: FakeTransport, tokens: TokenSet | None) -> PortalHttp:
    storage = InMemoryTokenStorage(tokens)
    auth = AuthManager(_config(), storage=storage, transport=transport)
    return PortalHttp(_config(), auth, transport=transport)


def test_injects_bearer_header_and_joins_base_url() -> None:
    transport = FakeTransport().add(json_response({"ok": True}))
    portal = _portal(transport, _fresh())

    body = portal.request_json("GET", "/items")

    assert body == {"ok": True}
    sent = transport.requests[0]
    assert sent.url == "https://portal.example.com/api/items"
    assert sent.headers.get("Authorization") == "Bearer access-old"
    assert sent.headers.get("Accept") == "application/json"


def test_404_raises_notfound() -> None:
    transport = FakeTransport().add(json_response({"message": "missing"}, status=404))
    portal = _portal(transport, _fresh())

    with pytest.raises(NotFoundError):
        portal.request_json("GET", "/items/xyz")


def test_409_raises_conflict_with_code() -> None:
    transport = FakeTransport().add(json_response({"code": "conflict"}, status=409))
    portal = _portal(transport, _fresh())

    with pytest.raises(ConflictError) as exc:
        portal.request_json("POST", "/items", json={})
    assert exc.value.code == "conflict"


def test_422_raises_validation() -> None:
    transport = FakeTransport().add(
        json_response({"errors": ["title required"]}, status=422)
    )
    portal = _portal(transport, _fresh())

    with pytest.raises(ValidationError):
        portal.request_json("POST", "/items", json={})


def test_401_triggers_one_shot_refresh_then_succeeds() -> None:
    """When the portal returns 401, the http layer should ask the auth
    manager to refresh and re-send exactly once. Verified by counting
    portal calls and confirming the second call carries the new token.
    """
    config = _config()
    discovery = {
        "issuer": config.oidc_issuer,
        "authorization_endpoint": f"{config.oidc_issuer}/auth",
        "token_endpoint": f"{config.oidc_issuer}/token",
    }
    portal_calls = {"n": 0}

    def handler(request: TransportRequest) -> TransportResponse:
        if request.url.endswith("/.well-known/openid-configuration"):
            return json_response(discovery)
        if request.url.endswith("/token"):
            return json_response(
                {
                    "access_token": "access-new",
                    "refresh_token": "refresh-2",
                    "expires_in": 300,
                    "refresh_expires_in": 3600,
                    "token_type": "Bearer",
                }
            )
        portal_calls["n"] += 1
        if portal_calls["n"] == 1:
            return json_response({"message": "expired"}, status=401)
        return json_response({"ok": True})

    transport = FakeTransport(handler=handler)
    portal = _portal(transport, _fresh())

    body = portal.request_json("GET", "/items")

    assert body == {"ok": True}
    assert portal_calls["n"] == 2
    api_requests = [r for r in transport.requests if "/api/items" in r.url]
    assert api_requests[0].headers.get("Authorization") == "Bearer access-old"
    assert api_requests[1].headers.get("Authorization") == "Bearer access-new"


def test_500_raises_generic_portalerror() -> None:
    transport = FakeTransport().add(text_response("kaboom", status=500))
    portal = _portal(transport, _fresh())

    with pytest.raises(PortalError) as exc:
        portal.request_json("GET", "/items")
    # Generic, not auth/validation/conflict/notfound.
    assert type(exc.value) is PortalError
    assert exc.value.status == 500


class TestErrorsCarryThePortalsReason:
    """A status code on its own is close to useless to a user.

    The raster publish failed for weeks-worth-of-confusion reasons
    behind a bare "HTTP 400" while the portal had been answering
    "Conversion failed: ..." the whole time.
    """

    def _message(self, body: object, status: int = 400) -> str:
        transport = FakeTransport().add(json_response(body, status=status))
        portal = _portal(transport, _fresh())
        with pytest.raises(PortalError) as exc:
            portal.request_json("POST", "/items/x/tile-layer/finalize", json={})
        return str(exc.value)

    def test_a_hand_thrown_message_is_included(self) -> None:
        text = self._message(
            {
                "statusCode": 400,
                "error": "Bad Request",
                "message": "Conversion failed: NoSuchKey",
            }
        )
        assert "Conversion failed: NoSuchKey" in text

    def test_a_validation_list_is_joined(self) -> None:
        # Nest's request validation answers with a list of failures.
        text = self._message(
            {
                "statusCode": 400,
                "message": ["title should not be empty", "access must be valid"],
            }
        )
        assert "title should not be empty" in text
        assert "access must be valid" in text

    def test_the_status_is_still_there(self) -> None:
        assert "400" in self._message({"message": "nope"})

    def test_a_plain_text_body_is_included(self) -> None:
        transport = FakeTransport().add(text_response("upstream exploded", status=502))
        portal = _portal(transport, _fresh())
        with pytest.raises(PortalError) as exc:
            portal.request_json("GET", "/items")
        assert "upstream exploded" in str(exc.value)

    def test_a_body_with_no_message_still_reads_cleanly(self) -> None:
        text = self._message({"statusCode": 400, "error": "Bad Request"})
        assert "400" in text
        assert text.endswith("400")

    def test_a_runaway_message_is_truncated(self) -> None:
        # An error body is not a log sink; a megabyte of it in a
        # message box helps nobody.
        text = self._message({"message": "x" * 5000})
        assert len(text) < 600


def test_auth_error_when_not_signed_in() -> None:
    transport = FakeTransport().add(json_response({}))
    portal = _portal(transport, None)

    with pytest.raises(AuthError):
        portal.request_json("GET", "/items")
    # The request never left the client.
    assert transport.requests == []


def test_transport_error_maps_to_portalerror_without_status() -> None:
    # DNS failure / refused connection: no HTTP response exists, so
    # status must be None to distinguish it from every mapped error.
    transport = FakeTransport().add_exception(TransportError("connection refused"))
    portal = _portal(transport, _fresh())

    with pytest.raises(PortalError) as exc:
        portal.request_json("GET", "/items")
    assert type(exc.value) is PortalError
    assert exc.value.status is None
    assert "connection refused" in str(exc.value)


def test_204_and_empty_body_return_none() -> None:
    transport = (
        FakeTransport()
        .add(TransportResponse(status=204))
        .add(TransportResponse(status=200, body=b""))
    )
    portal = _portal(transport, _fresh())

    assert portal.request_json("DELETE", "/items/abc") is None
    assert portal.request_json("GET", "/items/abc") is None


def test_non_json_2xx_raises_portalerror() -> None:
    transport = FakeTransport().add(text_response("<html>proxy page</html>"))
    portal = _portal(transport, _fresh())

    with pytest.raises(PortalError) as exc:
        portal.request_json("GET", "/items")
    assert "non-JSON" in str(exc.value)


def test_expect_status_mismatch_raises_even_on_2xx() -> None:
    transport = FakeTransport().add(json_response({"ok": True}, status=200))
    portal = _portal(transport, _fresh())

    with pytest.raises(PortalError):
        portal.request_json("POST", "/items", json={}, expect_status=201)


def test_query_params_encode_lists_and_scalars() -> None:
    transport = FakeTransport().add(json_response([]))
    portal = _portal(transport, _fresh())

    portal.request_json(
        "GET", "/items", params={"type": ["data_layer", "map"], "limit": 10, "skip": None}
    )

    url = transport.requests[0].url
    assert url == (
        "https://portal.example.com/api/items"
        "?type=data_layer&type=map&limit=10"
    )


def test_multipart_sends_boundary_content_type_and_body() -> None:
    transport = FakeTransport().add(json_response({"ok": True}))
    portal = _portal(transport, _fresh())

    portal.request_multipart(
        "POST",
        "/ingest/stage",
        files={"file": ("parcels.gpkg", b"bytes", "application/octet-stream")},
        data={"mode": "replace"},
    )

    sent = transport.requests[0]
    content_type = sent.headers.get("Content-Type", "")
    assert content_type.startswith("multipart/form-data; boundary=")
    assert sent.body is not None
    assert b'filename="parcels.gpkg"' in sent.body
    assert b'name="mode"' in sent.body
    # Default upload timeout is the long one, not the JSON default.
    assert sent.timeout == 600.0
