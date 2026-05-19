# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for AuthManager refresh + 401-retry behavior."""

from __future__ import annotations

import time

import httpx
import pytest

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.auth.storage import InMemoryTokenStorage
from gratisgis_client.auth.tokens import TokenSet
from gratisgis_client.config import PortalConfig
from gratisgis_client.errors import AuthError


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


def _mock_transport(handler):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_access_token_returns_cached_when_fresh() -> None:
    c = _config()
    storage = InMemoryTokenStorage(_fresh_tokens())
    http = httpx.AsyncClient(transport=_mock_transport(lambda req: httpx.Response(500)))
    mgr = AuthManager(c, storage=storage, http=http)

    token = await mgr.access_token()
    assert token == "access-old"
    await mgr.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_stale_access_token_triggers_refresh() -> None:
    c = _config()
    storage = InMemoryTokenStorage(_stale_tokens())

    discovery = _discovery_doc(c)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery)
        if req.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "access-new",
                    "refresh_token": "refresh-2",
                    "expires_in": 300,
                    "refresh_expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    mgr = AuthManager(c, storage=storage, http=http)

    token = await mgr.access_token()
    assert token == "access-new"

    # Persisted: a second manager pointed at the same storage sees the new token.
    storage2 = InMemoryTokenStorage()
    storage2._tokens = await storage.load()  # type: ignore[attr-defined]
    assert storage2._tokens is not None  # type: ignore[attr-defined]
    assert storage2._tokens.refresh_token == "refresh-2"  # type: ignore[attr-defined]

    await mgr.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_expired_refresh_token_raises_authrror_on_access_token() -> None:
    c = _config()
    storage = InMemoryTokenStorage(_expired_refresh_tokens())
    http = httpx.AsyncClient(transport=_mock_transport(lambda req: httpx.Response(500)))
    mgr = AuthManager(c, storage=storage, http=http)

    with pytest.raises(AuthError):
        await mgr.access_token()
    await mgr.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_keycloak_refresh_400_raises_authrror() -> None:
    c = _config()
    storage = InMemoryTokenStorage(_stale_tokens())

    discovery = _discovery_doc(c)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery)
        if req.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "stale"}
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    mgr = AuthManager(c, storage=storage, http=http)

    with pytest.raises(AuthError):
        await mgr.access_token()
    await mgr.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_concurrent_refresh_under_lock() -> None:
    """Many coroutines hitting access_token() simultaneously on a stale
    token should result in exactly one network refresh.
    """
    import asyncio

    c = _config()
    storage = InMemoryTokenStorage(_stale_tokens())
    discovery = _discovery_doc(c)

    calls = {"discovery": 0, "token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            calls["discovery"] += 1
            return httpx.Response(200, json=discovery)
        if req.url.path.endswith("/protocol/openid-connect/token"):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "access-new",
                    "refresh_token": "refresh-2",
                    "expires_in": 300,
                    "refresh_expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    mgr = AuthManager(c, storage=storage, http=http)

    results = await asyncio.gather(*[mgr.access_token() for _ in range(10)])
    assert all(r == "access-new" for r in results)
    assert calls["token"] == 1
    await mgr.close()
    await http.aclose()
