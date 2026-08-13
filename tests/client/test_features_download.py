# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the features.download_geojson endpoint (Phase 7)."""
from __future__ import annotations

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.features import FeaturesEndpoint
from gratisgis_client.http import PortalHttp
from tests.client.transport_stub import FakeTransport, json_response, path_of

PORTAL_URL = "https://portal.example"


class _FakeAuth:
    def access_token(self) -> str:
        return "fake-token"

    def force_refresh(self) -> str:  # pragma: no cover
        return "fake-token"


def _endpoint(transport: FakeTransport) -> FeaturesEndpoint:
    config = PortalConfig(
        portal_url=PORTAL_URL,
        keycloak_url=PORTAL_URL,
        realm="gratis-gis",
        client_id="qgis-plugin",
        verify_tls=False,
    )
    return FeaturesEndpoint(PortalHttp(config, _FakeAuth(), transport=transport))


class TestDownloadGeoJson:
    def test_returns_feature_collection_dict(self) -> None:
        transport = FakeTransport().add(
            json_response(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [0, 0]},
                            "properties": {"name": "X"},
                        }
                    ],
                }
            )
        )
        endpoint = _endpoint(transport)
        body = endpoint.download_geojson(item_id="i", layer_id="l")
        assert path_of(transport.requests[0]) == "/api/items/i/layers/l/geojson"
        assert body["type"] == "FeatureCollection"
        assert len(body["features"]) == 1

    def test_bbox_is_passed_as_comma_separated_query(self) -> None:
        # The portal expects bbox as one comma-separated string in
        # min-lng, min-lat, max-lng, max-lat order. Pin the
        # serialization so a future param-typing refactor doesn't
        # silently break clipped clones.
        transport = FakeTransport().add(
            json_response({"type": "FeatureCollection", "features": []})
        )
        endpoint = _endpoint(transport)
        endpoint.download_geojson(
            item_id="i",
            layer_id="l",
            bbox=(-80.5, 38.2, -80.4, 38.3),
        )
        url = transport.requests[0].url
        assert url == (
            f"{PORTAL_URL}/api/items/i/layers/l/geojson"
            "?bbox=-80.5%2C38.2%2C-80.4%2C38.3"
        )

    def test_non_dict_body_returns_empty_collection(self) -> None:
        # Defense in depth: an upstream proxy returning a string
        # body shouldn't crash the clone dialog.
        transport = FakeTransport().add(json_response("oops"))
        endpoint = _endpoint(transport)
        body = endpoint.download_geojson(item_id="i", layer_id="l")
        assert body == {"type": "FeatureCollection", "features": []}
