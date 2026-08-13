# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the storage + tile-layer endpoint wrappers."""
from __future__ import annotations

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.storage import StorageEndpoint
from gratisgis_client.endpoints.tile_layer import TileLayerEndpoint
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


def _http(transport: FakeTransport) -> PortalHttp:
    config = PortalConfig(
        portal_url=PORTAL_URL,
        keycloak_url=PORTAL_URL,
        realm="gratis-gis",
        client_id="qgis-plugin",
        verify_tls=False,
    )
    return PortalHttp(config, _FakeAuth(), transport=transport)


class TestPresignUpload:
    def test_presign_upload_parses_url_and_max_bytes(self) -> None:
        # The dialog uses uploadUrl for the direct PUT and maxBytes
        # to refuse oversized files before initiating the PUT. Pin
        # the alias mapping so a portal-side rename breaks here
        # instead of at runtime.
        transport = FakeTransport().add(
            json_response(
                {
                    "uploadUrl": "https://minio.example/bucket/foo?sig=abc",
                    "publicUrl": "https://portal.example/api/storage/private/item-tile-layer/foo",
                    "key": "abc-123",
                    "maxBytes": 10_737_418_240,
                }
            )
        )
        endpoint = StorageEndpoint(_http(transport))
        result = endpoint.presign_upload(
            kind="item-tile-layer", content_type="application/octet-stream"
        )
        assert path_of(transport.requests[0]) == "/api/storage/presign-upload"
        assert result.upload_url.startswith("https://minio.example")
        assert result.public_url.endswith("/abc-123") or "abc-123" in result.key
        assert result.key == "abc-123"
        assert result.max_bytes == 10_737_418_240

    def test_presign_upload_sends_kind_and_content_type(self) -> None:
        transport = FakeTransport().add(
            json_response(
                {
                    "uploadUrl": "x",
                    "publicUrl": "y",
                    "key": "k",
                    "maxBytes": 1,
                }
            )
        )
        endpoint = StorageEndpoint(_http(transport))
        endpoint.presign_upload(kind="item-file", content_type="application/pdf")
        assert body_json(transport.requests[0]) == {
            "kind": "item-file",
            "contentType": "application/pdf",
        }

    def test_presign_upload_declares_size_when_given(self) -> None:
        # sizeBytes lets the portal enforce its per-kind cap at
        # presign time and bake Content-Length into the signature;
        # pin the alias so a rename breaks here, not on upload day.
        transport = FakeTransport().add(
            json_response(
                {
                    "uploadUrl": "x",
                    "publicUrl": "y",
                    "key": "k",
                    "maxBytes": 1,
                }
            )
        )
        endpoint = StorageEndpoint(_http(transport))
        endpoint.presign_upload(
            kind="item-tile-layer",
            content_type="application/octet-stream",
            size_bytes=123_456,
        )
        assert body_json(transport.requests[0]) == {
            "kind": "item-tile-layer",
            "contentType": "application/octet-stream",
            "sizeBytes": 123_456,
        }


class TestCheckTileLayerSpace:
    def test_check_space_returns_ok_true(self) -> None:
        transport = FakeTransport().add(json_response({"ok": True}))
        endpoint = StorageEndpoint(_http(transport))
        result = endpoint.check_tile_layer_space(
            file_name="parcels.pmtiles",
            size_bytes=1024,
        )
        assert path_of(transport.requests[0]) == "/api/tile-layer/check-space"
        assert result.ok is True
        assert result.reason is None

    def test_check_space_surfaces_reason_on_refuse(self) -> None:
        # The dialog shows the reason so the user knows whether
        # to free disk on the portal host or pick a smaller file.
        transport = FakeTransport().add(
            json_response({"ok": False, "reason": "Only 2.1 GB free; 4.0 GB needed."})
        )
        endpoint = StorageEndpoint(_http(transport))
        result = endpoint.check_tile_layer_space(
            file_name="big.tif",
            size_bytes=2_000_000_000,
        )
        assert result.ok is False
        assert result.reason is not None and "GB" in result.reason


class TestTileLayerFinalize:
    def test_finalize_posts_expected_body(self) -> None:
        transport = FakeTransport().add(
            json_response(
                {
                    "data": {
                        "version": 1,
                        "format": "pmtiles",
                        "fileName": "parcels.pmtiles",
                        "sizeBytes": 1024,
                        "storageKey": "item-tile-layer/abc",
                        "processingState": "ready",
                    }
                }
            )
        )
        endpoint = TileLayerEndpoint(_http(transport))
        result = endpoint.finalize(
            item_id="item-1",
            storage_key="item-tile-layer/abc",
            storage_url="https://portal.example/api/storage/private/item-tile-layer/abc",
            file_name="parcels.pmtiles",
            size_bytes=1024,
        )
        sent = transport.requests[0]
        assert path_of(sent) == "/api/items/item-1/tile-layer/finalize"
        assert body_json(sent) == {
            "storageKey": "item-tile-layer/abc",
            "storageUrl": "https://portal.example/api/storage/private/item-tile-layer/abc",
            "fileName": "parcels.pmtiles",
            "sizeBytes": 1024,
        }
        assert "data" in result

    def test_retry_pyramid_hits_expected_route(self) -> None:
        transport = FakeTransport().add(
            json_response({"data": {"processingState": "cog-ready"}})
        )
        endpoint = TileLayerEndpoint(_http(transport))
        result = endpoint.retry_pyramid(item_id="item-1")
        assert path_of(transport.requests[0]) == "/api/items/item-1/tile-layer/retry-pyramid"
        assert "data" in result
