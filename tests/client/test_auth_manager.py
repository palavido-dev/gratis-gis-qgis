# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for AuthManager refresh behavior and refresh-lock dedupe."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.auth.storage import InMemoryTokenStorage
from gratisgis_client.auth.tokens import TokenSet
from gratisgis_client.config import PortalConfig
from gratisgis_client.errors import AuthError
from gratisgis_client.transport import TransportError, TransportRequest, TransportResponse
from tests.client.transport_stub import FakeTransport, json_response


def _config() -> PortalConfig:
    return PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )


def _fresh_tokens() -> TokenSet:
    return TokenSet(
        access_token="access-old",
        refresh_token="refresh-1",
        access_expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 7200,
    )


def _stale_tokens() -> TokenSet:
    return TokenSet(
        access_token="access-old",
        refresh_token="refresh-1",
        access_expires_at=time.time() - 10,
        refresh_expires_at=time.time() + 3600,
    )


def _expired_refresh_tokens() -> TokenSet:
    return TokenSet(
        access_token="access-old",
        refresh_token="refresh-1",
        access_expires_at=time.time() - 10,
        refresh_expires_at=time.time() - 1,
    )


def _discovery_doc(c: PortalConfig) -> dict[str, str]:
    issuer = c.oidc_issuer
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/protocol/openid-connect/auth",
        "token_endpoint": f"{issuer}/protocol/openid-connect/token",
        "end_session_endpoint": f"{issuer}/protocol/openid-connect/logout",
    }


def _token_success() -> TransportResponse:
    return json_response(
        {
            "access_token": "access-new",
            "refresh_token": "refresh-2",
            "expires_in": 300,
            "refresh_expires_in": 3600,
            "token_type": "Bearer",
        }
    )


def test_access_token_returns_cached_when_fresh() -> None:
    transport = FakeTransport()  # any network call would raise
    mgr = AuthManager(_config(), storage=InMemoryTokenStorage(_fresh_tokens()), transport=transport)

    assert mgr.access_token() == "access-old"
    assert transport.requests == []


def test_stale_access_token_triggers_refresh_and_persists() -> None:
    c = _config()
    discovery = _discovery_doc(c)

    def handler(request: TransportRequest) -> TransportResponse:
        if request.url.endswith("/.well-known/openid-configuration"):
            return json_response(discovery)
        if request.url.endswith("/protocol/openid-connect/token"):
            return _token_success()
        raise AssertionError(f"unexpected url {request.url}")

    storage = InMemoryTokenStorage(_stale_tokens())
    mgr = AuthManager(c, storage=storage, transport=FakeTransport(handler=handler))

    assert mgr.access_token() == "access-new"

    # Persisted: a second manager pointed at the same storage sees the
    # rotated refresh token without any network traffic.
    mgr2 = AuthManager(c, storage=storage, transport=FakeTransport())
    assert mgr2.access_token() == "access-new"
    stored = storage.load()
    assert stored is not None
    assert stored.refresh_token == "refresh-2"


def test_refresh_posts_form_encoded_grant() -> None:
    c = _config()
    discovery = _discovery_doc(c)

    def handler(request: TransportRequest) -> TransportResponse:
        if request.url.endswith("/.well-known/openid-configuration"):
            return json_response(discovery)
        return _token_success()

    transport = FakeTransport(handler=handler)
    mgr = AuthManager(c, storage=InMemoryTokenStorage(_stale_tokens()), transport=transport)
    mgr.access_token()

    token_request = transport.requests[-1]
    assert token_request.method == "POST"
    assert token_request.headers.get("Content-Type") == "application/x-www-form-urlencoded"
    assert isinstance(token_request.body, bytes)
    sent = token_request.body.decode("ascii")
    assert "grant_type=refresh_token" in sent
    assert "refresh_token=refresh-1" in sent
    assert "client_id=qgis-plugin" in sent


def test_expired_refresh_token_raises_autherror_on_access_token() -> None:
    mgr = AuthManager(
        _config(),
        storage=InMemoryTokenStorage(_expired_refresh_tokens()),
        transport=FakeTransport(),
    )
    with pytest.raises(AuthError):
        mgr.access_token()


def test_keycloak_refresh_400_raises_autherror() -> None:
    c = _config()
    discovery = _discovery_doc(c)

    def handler(request: TransportRequest) -> TransportResponse:
        if request.url.endswith("/.well-known/openid-configuration"):
            return json_response(discovery)
        return json_response(
            {"error": "invalid_grant", "error_description": "stale"}, status=400
        )

    mgr = AuthManager(
        c,
        storage=InMemoryTokenStorage(_stale_tokens()),
        transport=FakeTransport(handler=handler),
    )
    with pytest.raises(AuthError):
        mgr.access_token()


def test_network_failure_during_refresh_raises_autherror() -> None:
    # A dead Keycloak must surface as "sign-in problem", not leak a
    # transport exception through the public surface.
    mgr = AuthManager(
        _config(),
        storage=InMemoryTokenStorage(_stale_tokens()),
        transport=FakeTransport().add_exception(TransportError("dns failure")),
    )
    with pytest.raises(AuthError):
        mgr.access_token()


def test_not_signed_in_raises_autherror() -> None:
    mgr = AuthManager(_config(), storage=InMemoryTokenStorage(), transport=FakeTransport())
    with pytest.raises(AuthError):
        mgr.access_token()
    assert mgr.is_signed_in() is False


def test_concurrent_refresh_under_lock() -> None:
    """Many threads hitting access_token() simultaneously on a stale
    token should result in exactly one network refresh.
    """
    c = _config()
    discovery = _discovery_doc(c)
    calls = {"token": 0}

    def handler(request: TransportRequest) -> TransportResponse:
        if request.url.endswith("/.well-known/openid-configuration"):
            return json_response(discovery)
        calls["token"] += 1
        # Widen the race window so losers actually queue on the lock.
        time.sleep(0.05)
        return _token_success()

    mgr = AuthManager(
        c,
        storage=InMemoryTokenStorage(_stale_tokens()),
        transport=FakeTransport(handler=handler),
    )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: mgr.access_token(), range(10)))

    assert all(r == "access-new" for r in results)
    assert calls["token"] == 1


def test_discovery_document_is_cached() -> None:
    c = _config()
    calls = {"discovery": 0}

    def handler(request: TransportRequest) -> TransportResponse:
        calls["discovery"] += 1
        return json_response(_discovery_doc(c))

    mgr = AuthManager(c, storage=InMemoryTokenStorage(), transport=FakeTransport(handler=handler))
    mgr.discover()
    mgr.discover()
    assert calls["discovery"] == 1
