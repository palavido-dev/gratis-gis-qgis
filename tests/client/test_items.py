# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the items endpoint wrapper."""

from __future__ import annotations

import time

import httpx
import pytest

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.auth.storage import InMemoryTokenStorage
from gratisgis_client.auth.tokens import TokenSet
from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.items import ItemsEndpoint
from gratisgis_client.http import PortalHttp


def _config() -> PortalConfig:
    return PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
    )


def _fresh_tokens() -> TokenSet:
    return TokenSet(
        access_token="access-token",
        refresh_token="refresh-token",
        access_expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 7200,
    )


def _summary_payload(id_: str = "abc") -> dict[str, object]:
    return {
        "id": id_,
        "type": "data_layer",
        "title": "Example layer",
        "summary": None,
        "description": None,
        "tags": ["sample"],
        "access": "org",
        "ownerId": "user-1",
        "ownerUsername": "alice",
        "orgId": "org-1",
        "folderId": None,
        "thumbnailUrl": None,
        "createdAt": "2026-05-19T00:00:00Z",
        "updatedAt": "2026-05-19T00:00:00Z",
    }


def _item_payload(id_: str = "abc") -> dict[str, object]:
    base = _summary_payload(id_)
    base["data"] = {"layers": [{"id": "l1", "name": "Stuff"}]}
    base["license"] = "CC-BY-4.0"
    base["thumbnailDesign"] = None
    return base


def _setup_endpoint(handler):  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    storage = InMemoryTokenStorage(_fresh_tokens())
    auth_http = httpx.AsyncClient(transport=transport)
    auth = AuthManager(_config(), storage=storage, http=auth_http)
    api_client = httpx.AsyncClient(base_url=_config().api_base, transport=transport)
    portal = PortalHttp(_config(), auth, client=api_client)
    return ItemsEndpoint(portal), auth, auth_http, api_client


@pytest.mark.asyncio
async def test_list_parses_bare_array() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/items"
        return httpx.Response(200, json=[_summary_payload("a"), _summary_payload("b")])

    items, auth, ah, api = _setup_endpoint(handler)
    result = await items.list()
    assert len(result.items) == 2
    assert {i.id for i in result.items} == {"a", "b"}
    await auth.close()
    await ah.aclose()
    await api.aclose()


@pytest.mark.asyncio
async def test_list_parses_paginated_envelope() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [_summary_payload("a")], "total": 1, "nextCursor": None}
        )

    items, auth, ah, api = _setup_endpoint(handler)
    result = await items.list()
    assert len(result.items) == 1
    assert result.total == 1
    assert result.next_cursor is None
    await auth.close()
    await ah.aclose()
    await api.aclose()


@pytest.mark.asyncio
async def test_list_passes_filters_as_query_params() -> None:
    seen: dict[str, list[str]] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        for key in req.url.params.multi_items():
            seen.setdefault(key[0], []).append(key[1])
        return httpx.Response(200, json=[])

    items, auth, ah, api = _setup_endpoint(handler)
    await items.list(types=["data_layer", "map"], access="org", limit=10, query="parcel")
    assert seen.get("type") == ["data_layer", "map"]
    assert seen.get("access") == ["org"]
    assert seen.get("limit") == ["10"]
    assert seen.get("q") == ["parcel"]
    await auth.close()
    await ah.aclose()
    await api.aclose()


@pytest.mark.asyncio
async def test_get_returns_full_item_envelope() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/items/abc"
        return httpx.Response(200, json=_item_payload("abc"))

    items, auth, ah, api = _setup_endpoint(handler)
    item = await items.get("abc")
    assert item.id == "abc"
    assert item.data["layers"][0]["id"] == "l1"
    assert item.license == "CC-BY-4.0"
    await auth.close()
    await ah.aclose()
    await api.aclose()


@pytest.mark.asyncio
async def test_create_posts_full_envelope() -> None:
    seen_bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        seen_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_item_payload("new"))

    items, auth, ah, api = _setup_endpoint(handler)
    result = await items.create(
        type="data_layer",
        title="New layer",
        data={"layers": []},
        description="testing",
        tags=["t1"],
        access="org",
    )
    assert result.id == "new"
    assert seen_bodies[0]["type"] == "data_layer"
    assert seen_bodies[0]["title"] == "New layer"
    assert seen_bodies[0]["access"] == "org"
    await auth.close()
    await ah.aclose()
    await api.aclose()


@pytest.mark.asyncio
async def test_update_sends_only_provided_fields() -> None:
    seen_bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        seen_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_item_payload("abc"))

    items, auth, ah, api = _setup_endpoint(handler)
    await items.update("abc", title="Renamed")
    assert seen_bodies[0] == {"title": "Renamed"}
    await auth.close()
    await ah.aclose()
    await api.aclose()
