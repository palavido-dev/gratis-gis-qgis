# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the features.download_geojson endpoint (Phase 7)."""
from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.features import FeaturesEndpoint
from gratisgis_client.http import PortalHttp

PORTAL_URL = "https://portal.example"


class _FakeAuth:
    async def access_token(self) -> str:
        return "fake-token"

    async def force_refresh(self) -> str:  # pragma: no cover
        return "fake-token"


@pytest.fixture
def http() -> PortalHttp:
    config = PortalConfig(
        portal_url=PORTAL_URL,
        keycloak_url=PORTAL_URL,
        realm="gratis-gis",
        client_id="qgis-plugin",
        verify_tls=False,
    )
    return PortalHttp(config, _FakeAuth())  # type: ignore[arg-type]


class TestDownloadGeoJson:
    @pytest.mark.asyncio
    async def test_returns_feature_collection_dict(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{PORTAL_URL}/api/items/i/layers/l/geojson",
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [0, 0]},
                        "properties": {"name": "X"},
                    }
                ],
            },
        )
        endpoint = FeaturesEndpoint(http)
        body = await endpoint.download_geojson(item_id="i", layer_id="l")
        assert body["type"] == "FeatureCollection"
        assert len(body["features"]) == 1

    @pytest.mark.asyncio
    async def test_bbox_is_passed_as_comma_separated_query(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # The portal expects bbox as one comma-separated string in
        # min-lng, min-lat, max-lng, max-lat order. Pin the
        # serialization so a future param-typing refactor doesn't
        # silently break clipped clones.
        httpx_mock.add_response(
            method="GET",
            url=(
                f"{PORTAL_URL}/api/items/i/layers/l/geojson"
                "?bbox=-80.5%2C38.2%2C-80.4%2C38.3"
            ),
            json={"type": "FeatureCollection", "features": []},
        )
        endpoint = FeaturesEndpoint(http)
        await endpoint.download_geojson(
            item_id="i",
            layer_id="l",
            bbox=(-80.5, 38.2, -80.4, 38.3),
        )

    @pytest.mark.asyncio
    async def test_non_dict_body_returns_empty_collection(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # Defense in depth: an upstream proxy returning a string
        # body shouldn't crash the clone dialog.
        httpx_mock.add_response(
            method="GET",
            url=f"{PORTAL_URL}/api/items/i/layers/l/geojson",
            json="oops",
        )
        endpoint = FeaturesEndpoint(http)
        body = await endpoint.download_geojson(item_id="i", layer_id="l")
        assert body == {"type": "FeatureCollection", "features": []}
