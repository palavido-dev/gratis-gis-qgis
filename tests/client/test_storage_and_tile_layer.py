# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the storage + tile-layer endpoint wrappers."""
from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.storage import StorageEndpoint
from gratisgis_client.endpoints.tile_layer import TileLayerEndpoint
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


class TestPresignUpload:
    @pytest.mark.asyncio
    async def test_presign_upload_parses_url_and_max_bytes(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # The dialog uses uploadUrl for the direct PUT and maxBytes
        # to refuse oversized files before initiating the PUT. Pin
        # the alias mapping so a portal-side rename breaks here
        # instead of at runtime.
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/storage/presign-upload",
            json={
                "uploadUrl": "https://minio.example/bucket/foo?sig=abc",
                "publicUrl": "https://portal.example/api/storage/private/item-tile-layer/foo",
                "key": "abc-123",
                "maxBytes": 10_737_418_240,
            },
        )
        endpoint = StorageEndpoint(http)
        result = await endpoint.presign_upload(
            kind="item-tile-layer", content_type="application/octet-stream"
        )
        assert result.upload_url.startswith("https://minio.example")
        assert result.public_url.endswith("/abc-123") or "abc-123" in result.key
        assert result.key == "abc-123"
        assert result.max_bytes == 10_737_418_240

    @pytest.mark.asyncio
    async def test_presign_upload_sends_kind_and_content_type(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/storage/presign-upload",
            json={
                "uploadUrl": "x",
                "publicUrl": "y",
                "key": "k",
                "maxBytes": 1,
            },
        )
        endpoint = StorageEndpoint(http)
        await endpoint.presign_upload(
            kind="item-file", content_type="application/pdf"
        )
        request = httpx_mock.get_request()
        assert request is not None
        sent = json.loads(request.content)
        assert sent == {"kind": "item-file", "contentType": "application/pdf"}


class TestCheckTileLayerSpace:
    @pytest.mark.asyncio
    async def test_check_space_returns_ok_true(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/tile-layer/check-space",
            json={"ok": True},
        )
        endpoint = StorageEndpoint(http)
        result = await endpoint.check_tile_layer_space(
            file_name="parcels.pmtiles", size_bytes=1024,
        )
        assert result.ok is True
        assert result.reason is None

    @pytest.mark.asyncio
    async def test_check_space_surfaces_reason_on_refuse(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # The dialog shows the reason so the user knows whether
        # to free disk on the portal host or pick a smaller file.
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/tile-layer/check-space",
            json={"ok": False, "reason": "Only 2.1 GB free; 4.0 GB needed."},
        )
        endpoint = StorageEndpoint(http)
        result = await endpoint.check_tile_layer_space(
            file_name="big.tif", size_bytes=2_000_000_000,
        )
        assert result.ok is False
        assert result.reason is not None and "GB" in result.reason


class TestTileLayerFinalize:
    @pytest.mark.asyncio
    async def test_finalize_posts_expected_body(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/items/item-1/tile-layer/finalize",
            json={
                "data": {
                    "version": 1,
                    "format": "pmtiles",
                    "fileName": "parcels.pmtiles",
                    "sizeBytes": 1024,
                    "storageKey": "item-tile-layer/abc",
                    "processingState": "ready",
                }
            },
        )
        endpoint = TileLayerEndpoint(http)
        result = await endpoint.finalize(
            item_id="item-1",
            storage_key="item-tile-layer/abc",
            storage_url="https://portal.example/api/storage/private/item-tile-layer/abc",
            file_name="parcels.pmtiles",
            size_bytes=1024,
        )
        request = httpx_mock.get_request()
        assert request is not None
        sent = json.loads(request.content)
        assert sent == {
            "storageKey": "item-tile-layer/abc",
            "storageUrl": "https://portal.example/api/storage/private/item-tile-layer/abc",
            "fileName": "parcels.pmtiles",
            "sizeBytes": 1024,
        }
        assert "data" in result

    @pytest.mark.asyncio
    async def test_retry_pyramid_hits_expected_route(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/items/item-1/tile-layer/retry-pyramid",
            json={"data": {"processingState": "cog-ready"}},
        )
        endpoint = TileLayerEndpoint(http)
        result = await endpoint.retry_pyramid(item_id="item-1")
        assert "data" in result
