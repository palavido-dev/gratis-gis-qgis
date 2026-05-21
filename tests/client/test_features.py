# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the features endpoint (per-feature CRUD).

Pin the wire shape against pytest-httpx mocks so a portal-side
rename can't silently break the QGIS edit-push flow.
"""
from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.features import FeatureIn, FeaturesEndpoint
from gratisgis_client.http import PortalHttp


PORTAL_URL = "https://portal.example"


class _FakeAuth:
    async def access_token(self) -> str:
        return "fake-token"

    async def force_refresh(self) -> str:  # pragma: no cover
        return "fake-token"


@pytest.fixture
def config() -> PortalConfig:
    return PortalConfig(
        portal_url=PORTAL_URL,
        keycloak_url=PORTAL_URL,
        realm="gratis-gis",
        client_id="qgis-plugin",
        verify_tls=False,
    )


@pytest.fixture
def http(config: PortalConfig) -> PortalHttp:
    return PortalHttp(config, _FakeAuth())  # type: ignore[arg-type]


class TestAppend:
    @pytest.mark.asyncio
    async def test_append_posts_features_array(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/items/i/layers/l/features",
            json={"inserted": 2},
        )
        endpoint = FeaturesEndpoint(http)
        result = await endpoint.append(
            item_id="i",
            layer_id="l",
            features=[
                FeatureIn(
                    geometry={"type": "Point", "coordinates": [0, 0]},
                    properties={"name": "A"},
                ),
                FeatureIn(
                    geometry={"type": "Point", "coordinates": [1, 1]},
                    properties={"name": "B"},
                ),
            ],
        )
        assert result.inserted == 2
        sent = json.loads(httpx_mock.get_request().content)
        assert sent == {
            "features": [
                {
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                    "properties": {"name": "A"},
                },
                {
                    "geometry": {"type": "Point", "coordinates": [1, 1]},
                    "properties": {"name": "B"},
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_append_uses_global_id_alias(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # The offline-sync path depends on the portal honoring
        # client-supplied globalId for dedupe; the field name on
        # the wire must be 'globalId', not snake_case.
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/items/i/layers/l/features",
            json={"inserted": 1},
        )
        endpoint = FeaturesEndpoint(http)
        await endpoint.append(
            item_id="i",
            layer_id="l",
            features=[FeatureIn(global_id="abc", properties={"k": 1})],
        )
        sent = json.loads(httpx_mock.get_request().content)
        assert sent["features"][0]["globalId"] == "abc"

    @pytest.mark.asyncio
    async def test_append_omits_none_geometry(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # Table layers send geometry-less inserts; the wire should
        # not carry an explicit null because the portal treats null
        # geometry as "clear" not "absent".
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/items/i/layers/l/features",
            json={"inserted": 1},
        )
        endpoint = FeaturesEndpoint(http)
        await endpoint.append(
            item_id="i",
            layer_id="l",
            features=[FeatureIn(properties={"k": 1})],
        )
        sent = json.loads(httpx_mock.get_request().content)
        assert "geometry" not in sent["features"][0]


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_patches_only_provided_fields(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="PATCH",
            url=f"{PORTAL_URL}/api/items/i/layers/l/features/f",
            json={"id": "f", "properties": {"k": "v"}},
        )
        endpoint = FeaturesEndpoint(http)
        result = await endpoint.update(
            item_id="i",
            layer_id="l",
            feature_id="f",
            properties={"k": "v"},
        )
        assert result.id == "f"
        sent = json.loads(httpx_mock.get_request().content)
        # The portal treats absent keys as 'no change'; omitting
        # geometry here means "don't touch geometry".
        assert sent == {"properties": {"k": "v"}}

    @pytest.mark.asyncio
    async def test_update_can_send_geometry_and_properties(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="PATCH",
            url=f"{PORTAL_URL}/api/items/i/layers/l/features/f",
            json={"id": "f"},
        )
        endpoint = FeaturesEndpoint(http)
        await endpoint.update(
            item_id="i",
            layer_id="l",
            feature_id="f",
            geometry={"type": "Point", "coordinates": [1, 2]},
            properties={"k": "v"},
        )
        sent = json.loads(httpx_mock.get_request().content)
        assert sent == {
            "geometry": {"type": "Point", "coordinates": [1, 2]},
            "properties": {"k": "v"},
        }


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_returns_none_on_204(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="DELETE",
            url=f"{PORTAL_URL}/api/items/i/layers/l/features/f",
            status_code=204,
        )
        endpoint = FeaturesEndpoint(http)
        result = await endpoint.delete(item_id="i", layer_id="l", feature_id="f")
        assert result is None
