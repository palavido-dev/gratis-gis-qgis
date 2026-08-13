# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the features endpoint (per-feature CRUD).

Pin the wire shape at the transport seam so a portal-side rename
can't silently break the QGIS edit-push flow.
"""
from __future__ import annotations

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.features import FeatureIn, FeaturesEndpoint
from gratisgis_client.http import PortalHttp
from tests.client.transport_stub import (
    FakeTransport,
    body_json,
    json_response,
    path_of,
)

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


class TestAppend:
    def test_append_posts_features_array(self) -> None:
        transport = FakeTransport().add(json_response({"inserted": 2}))
        endpoint = _endpoint(transport)
        result = endpoint.append(
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
        sent = transport.requests[0]
        assert path_of(sent) == "/api/items/i/layers/l/features"
        assert body_json(sent) == {
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

    def test_append_uses_global_id_alias(self) -> None:
        # The offline-sync path depends on the portal honoring
        # client-supplied globalId for dedupe; the field name on
        # the wire must be 'globalId', not snake_case.
        transport = FakeTransport().add(json_response({"inserted": 1}))
        endpoint = _endpoint(transport)
        endpoint.append(
            item_id="i",
            layer_id="l",
            features=[FeatureIn.from_api({"globalId": "abc", "properties": {"k": 1}})],
        )
        sent = body_json(transport.requests[0])
        assert isinstance(sent, dict)
        assert sent["features"][0]["globalId"] == "abc"

    def test_append_omits_none_geometry(self) -> None:
        # Table layers send geometry-less inserts; the wire should
        # not carry an explicit null because the portal treats null
        # geometry as "clear" not "absent".
        transport = FakeTransport().add(json_response({"inserted": 1}))
        endpoint = _endpoint(transport)
        endpoint.append(
            item_id="i",
            layer_id="l",
            features=[FeatureIn(properties={"k": 1})],
        )
        sent = body_json(transport.requests[0])
        assert isinstance(sent, dict)
        assert "geometry" not in sent["features"][0]
        assert "globalId" not in sent["features"][0]

    def test_append_non_dict_response_returns_zero_inserted(self) -> None:
        transport = FakeTransport().add(json_response([1, 2, 3]))
        endpoint = _endpoint(transport)
        result = endpoint.append(item_id="i", layer_id="l", features=[])
        assert result.inserted == 0

    def test_append_parses_global_ids_and_dedup_count(self) -> None:
        # globalIds is order-aligned with the request; the push-edits
        # flow reads element [0] of a one-feature append as the
        # portal-assigned id, so both the field name and the order
        # contract are pinned here.
        transport = FakeTransport().add(
            json_response(
                {"inserted": 1, "deduplicated": 1, "globalIds": ["gid-1", "gid-2"]}
            )
        )
        result = _endpoint(transport).append(
            item_id="i",
            layer_id="l",
            features=[FeatureIn(properties={"k": 1}), FeatureIn(properties={"k": 2})],
        )
        assert result.inserted == 1
        assert result.deduplicated == 1
        assert result.global_ids == ["gid-1", "gid-2"]

    def test_append_tolerates_old_response_shape(self) -> None:
        # A portal predating the globalIds echo must not break the
        # client; ids just come back empty and write-back is skipped.
        transport = FakeTransport().add(json_response({"inserted": 1}))
        result = _endpoint(transport).append(
            item_id="i", layer_id="l", features=[FeatureIn(properties={"k": 1})]
        )
        assert result.inserted == 1
        assert result.deduplicated == 0
        assert result.global_ids == []


class TestUpdate:
    def test_update_patches_only_provided_fields(self) -> None:
        transport = FakeTransport().add(
            json_response({"id": "f", "properties": {"k": "v"}})
        )
        endpoint = _endpoint(transport)
        result = endpoint.update(
            item_id="i",
            layer_id="l",
            feature_id="f",
            properties={"k": "v"},
        )
        assert result.id == "f"
        sent = transport.requests[0]
        assert sent.method == "PATCH"
        assert path_of(sent) == "/api/items/i/layers/l/features/f"
        # The portal treats absent keys as 'no change'; omitting
        # geometry here means "don't touch geometry".
        assert body_json(sent) == {"properties": {"k": "v"}}

    def test_update_can_send_geometry_and_properties(self) -> None:
        transport = FakeTransport().add(json_response({"id": "f"}))
        endpoint = _endpoint(transport)
        endpoint.update(
            item_id="i",
            layer_id="l",
            feature_id="f",
            geometry={"type": "Point", "coordinates": [1, 2]},
            properties={"k": "v"},
        )
        assert body_json(transport.requests[0]) == {
            "geometry": {"type": "Point", "coordinates": [1, 2]},
            "properties": {"k": "v"},
        }


class TestDelete:
    def test_delete_returns_none_on_204(self) -> None:
        from gratisgis_client.transport import TransportResponse

        transport = FakeTransport().add(TransportResponse(status=204))
        endpoint = _endpoint(transport)
        # delete() returns None on 204; just making the call without
        # raising is the assertion.
        endpoint.delete(item_id="i", layer_id="l", feature_id="f")
        sent = transport.requests[0]
        assert sent.method == "DELETE"
        assert path_of(sent) == "/api/items/i/layers/l/features/f"
