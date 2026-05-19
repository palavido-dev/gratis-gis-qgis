# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PortalHttp: auth injection, 401 retry, error mapping."""

from __future__ import annotations

import time

import httpx
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


def _config() -> PortalConfig:
    return PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
    )


def _make_auth(tokens: TokenSet, transport: httpx.MockTransport) -> AuthManager:
    storage = InMemoryTokenStorage(tokens)
    http = httpx.AsyncClient(transport=transport)
    return AuthManager(_config(), storage=storage, http=http)


def _fresh() -> TokenSet:
    return TokenSet(
        access_token="access-old",
        refresh_token="refresh-1",
        access_expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 7200,
    )


@pytest.mark.asyncio
async def test_injects_bearer_header() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update({k: v for k, v in req.headers.items()})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    auth = _make_auth(_fresh(), transport)
    api_client = httpx.AsyncClient(
        base_url=_config().api_base, transport=httpx.MockTransport(handler)
    )
    portal = PortalHttp(_config(), auth, client=api_client)

    body = await portal.request_json("GET", "/items")
    assert body == {"ok": True}
    assert seen.get("authorization") == "Bearer access-old"

    await auth.close()
    await api_client.aclose()


@pytest.mark.asyncio
async def test_404_raises_notfound() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "missing"})

    transport = httpx.MockTransport(handler)
    auth = _make_auth(_fresh(), transport)
    api_client = httpx.AsyncClient(base_url=_config().api_base, transport=transport)
    portal = PortalHttp(_config(), auth, client=api_client)

    with pytest.raises(NotFoundError):
        await portal.request_json("GET", "/items/xyz")

    await auth.close()
    await api_client.aclose()


@pytest.mark.asyncio
async def test_409_raises_conflict() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "conflict"})

    transport = httpx.MockTransport(handler)
    auth = _make_auth(_fresh(), transport)
    api_client = httpx.AsyncClient(base_url=_config().api_base, transport=transport)
    portal = PortalHttp(_config(), auth, client=api_client)

    with pytest.raises(ConflictError) as exc:
        await portal.request_json("POST", "/items", json={})
    assert exc.value.code == "conflict"

    await auth.close()
    await api_client.aclose()


@pytest.mark.asyncio
async def test_422_raises_validation() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"errors": ["title required"]})

    transport = httpx.MockTransport(handler)
    auth = _make_auth(_fresh(), transport)
    api_client = httpx.AsyncClient(base_url=_config().api_base, transport=transport)
    portal = PortalHttp(_config(), auth, client=api_client)

    with pytest.raises(ValidationError):
        await portal.request_json("POST", "/items", json={})

    await auth.close()
    await api_client.aclose()


@pytest.mark.asyncio
async def test_401_triggers_one_shot_refresh_then_succeeds() -> None:
    """When the portal returns 401, the http layer should ask the auth
    manager to refresh and re-send exactly once. Verified by counting
    portal calls and confirming the second call carries the new token.
    """

    portal_calls = {"n": 0}
    seen_authz: list[str] = []
    discovery = {
        "issuer": _config().oidc_issuer,
        "authorization_endpoint": f"{_config().oidc_issuer}/auth",
        "token_endpoint": f"{_config().oidc_issuer}/token",
    }

    def auth_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery)
        if req.url.path.endswith("/token"):
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

    def portal_handler(req: httpx.Request) -> httpx.Response:
        portal_calls["n"] += 1
        seen_authz.append(req.headers.get("authorization", ""))
        if portal_calls["n"] == 1:
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json={"ok": True})

    auth_transport = httpx.MockTransport(auth_handler)
    storage = InMemoryTokenStorage(_fresh())
    auth_http = httpx.AsyncClient(transport=auth_transport)
    auth = AuthManager(_config(), storage=storage, http=auth_http)

    api_client = httpx.AsyncClient(
        base_url=_config().api_base, transport=httpx.MockTransport(portal_handler)
    )
    portal = PortalHttp(_config(), auth, client=api_client)

    body = await portal.request_json("GET", "/items")
    assert body == {"ok": True}
    assert portal_calls["n"] == 2
    assert seen_authz[0] == "Bearer access-old"
    assert seen_authz[1] == "Bearer access-new"

    await auth.close()
    await auth_http.aclose()
    await api_client.aclose()


@pytest.mark.asyncio
async def test_500_raises_generic_portalerror() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaboom")

    transport = httpx.MockTransport(handler)
    auth = _make_auth(_fresh(), transport)
    api_client = httpx.AsyncClient(base_url=_config().api_base, transport=transport)
    portal = PortalHttp(_config(), auth, client=api_client)

    with pytest.raises(PortalError) as exc:
        await portal.request_json("GET", "/items")
    # Generic, not auth/validation/conflict/notfound.
    assert type(exc.value) is PortalError

    await auth.close()
    await api_client.aclose()


@pytest.mark.asyncio
async def test_auth_error_when_not_signed_in() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    storage = InMemoryTokenStorage()
    auth_http = httpx.AsyncClient(transport=transport)
    auth = AuthManager(_config(), storage=storage, http=auth_http)
    api_client = httpx.AsyncClient(base_url=_config().api_base, transport=transport)
    portal = PortalHttp(_config(), auth, client=api_client)

    with pytest.raises(AuthError):
        await portal.request_json("GET", "/items")

    await auth.close()
    await auth_http.aclose()
    await api_client.aclose()
