# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ingest and import-jobs endpoint wrappers.

These pin the wire shape against pytest-httpx mocks so a portal-side
field rename can't silently break the plugin's publish flow.
"""
from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.import_jobs import ImportJobsEndpoint
from gratisgis_client.endpoints.ingest import IngestEndpoint
from gratisgis_client.http import PortalHttp


PORTAL_URL = "https://portal.example"


@pytest.fixture
def config() -> PortalConfig:
    return PortalConfig(
        portal_url=PORTAL_URL,
        keycloak_url=PORTAL_URL,
        realm="gratis-gis",
        client_id="qgis-plugin",
        verify_tls=False,
    )


class _FakeAuth:
    """Stand-in for AuthManager that hands out a static token.

    The real manager talks to Keycloak; for endpoint tests we just
    need request_json to attach an Authorization header.
    """

    async def access_token(self) -> str:
        return "fake-token"

    async def force_refresh(self) -> str:  # pragma: no cover
        return "fake-token"


@pytest.fixture
def http(config: PortalConfig) -> PortalHttp:
    # PortalHttp's type hint says AuthManager, but it only calls
    # access_token() / force_refresh(), so the fake satisfies it.
    return PortalHttp(config, _FakeAuth())  # type: ignore[arg-type]


# -----------------------------------------------------------
# Ingest endpoint
# -----------------------------------------------------------


class TestIngestStage:
    @pytest.mark.asyncio
    async def test_stage_parses_portal_envelope(
        self, http: PortalHttp, httpx_mock: HTTPXMock, tmp_path
    ) -> None:
        # Pinning the response shape against what the portal's
        # IngestController.stage actually returns. Adding a field
        # later is fine (extra='ignore'); renaming one breaks here
        # and we update both sides in lockstep.
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/ingest/stage",
            json={
                "stagingId": "stg_abc",
                "fileName": "parcels.gpkg",
                "sizeBytes": 1024,
                "expiresAt": "2026-05-20T12:00:00Z",
                "layers": [
                    {
                        "name": "parcels",
                        "geometryType": "polygon",
                        "fields": [{"name": "PIN", "type": "string"}],
                        "featureCount": 1000,
                    }
                ],
            },
        )
        gpkg = tmp_path / "parcels.gpkg"
        gpkg.write_bytes(b"fake-bytes")

        endpoint = IngestEndpoint(http)
        result = await endpoint.stage(file_path=str(gpkg))

        assert result.staging_id == "stg_abc"
        assert result.file_name == "parcels.gpkg"
        assert result.size_bytes == 1024
        assert len(result.layers) == 1
        layer = result.layers[0]
        assert layer.name == "parcels"
        assert layer.geometry_type == "polygon"
        assert layer.feature_count == 1000
        assert layer.fields[0].name == "PIN"

    @pytest.mark.asyncio
    async def test_stage_uses_file_basename_when_name_omitted(
        self, http: PortalHttp, httpx_mock: HTTPXMock, tmp_path
    ) -> None:
        # The portal's `originalName` should reflect the picked file
        # (so the wizard can show "parcels.gpkg" rather than the
        # opaque stagingId in the success message).
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/ingest/stage",
            json={"stagingId": "x", "fileName": "parcels.gpkg", "layers": []},
        )
        gpkg = tmp_path / "parcels.gpkg"
        gpkg.write_bytes(b"x")

        endpoint = IngestEndpoint(http)
        await endpoint.stage(file_path=str(gpkg))

        sent = httpx_mock.get_request()
        assert sent is not None
        # Multipart bodies contain the filename in the
        # Content-Disposition header of each part.
        assert b'filename="parcels.gpkg"' in sent.content

    @pytest.mark.asyncio
    async def test_stage_ignores_unknown_top_level_fields(
        self, http: PortalHttp, httpx_mock: HTTPXMock, tmp_path
    ) -> None:
        # Forward-compat: a portal that adds a `quotaRemaining` key
        # later should not break already-deployed plugins.
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/ingest/stage",
            json={
                "stagingId": "s",
                "fileName": "x",
                "layers": [],
                "quotaRemaining": 999,
            },
        )
        gpkg = tmp_path / "x.gpkg"
        gpkg.write_bytes(b"x")
        endpoint = IngestEndpoint(http)
        result = await endpoint.stage(file_path=str(gpkg))
        assert result.staging_id == "s"


# -----------------------------------------------------------
# Import-jobs endpoint
# -----------------------------------------------------------


class TestImportJobsEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_posts_expected_body(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/items/item-1/layers/parcels/import-jobs",
            json={
                "id": "job-1",
                "itemId": "item-1",
                "layerId": "parcels",
                "status": "queued",
                "mode": "replace",
                "sourceLayerName": "parcels",
                "sourceFileName": "parcels.gpkg",
                "totalFeatures": 1000,
                "processedFeatures": 0,
                "insertedFeatures": 0,
                "replacedFeatures": 0,
            },
        )

        endpoint = ImportJobsEndpoint(http)
        job = await endpoint.enqueue(
            item_id="item-1",
            layer_id="parcels",
            staging_id="stg_x",
            source_layer_name="parcels",
            mode="replace",
        )

        # Verify the wire shape the portal expects.
        sent = httpx_mock.get_request()
        assert sent is not None
        body = json.loads(sent.content)
        assert body == {
            "stagingId": "stg_x",
            "sourceLayerName": "parcels",
            "mode": "replace",
        }
        assert job.id == "job-1"
        assert job.status == "queued"
        assert job.mode == "replace"

    @pytest.mark.asyncio
    async def test_default_mode_is_replace(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # Replace is the wizard's default because it's what users
        # mean by 'publish'. Append is a power-user opt-in.
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/items/i/layers/l/import-jobs",
            json={
                "id": "j",
                "itemId": "i",
                "layerId": "l",
                "status": "queued",
                "mode": "replace",
            },
        )
        endpoint = ImportJobsEndpoint(http)
        await endpoint.enqueue(
            item_id="i",
            layer_id="l",
            staging_id="s",
            source_layer_name="src",
        )
        body = json.loads(httpx_mock.get_request().content)
        assert body["mode"] == "replace"


class TestImportJobProgress:
    @pytest.mark.asyncio
    async def test_get_returns_typed_job_with_progress_fields(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{PORTAL_URL}/api/import-jobs/job-1",
            json={
                "id": "job-1",
                "itemId": "i",
                "layerId": "l",
                "status": "running",
                "mode": "replace",
                "totalFeatures": 1000,
                "processedFeatures": 250,
                "insertedFeatures": 250,
            },
        )
        endpoint = ImportJobsEndpoint(http)
        job = await endpoint.get("job-1")
        assert job.status == "running"
        assert job.processed_features == 250
        assert job.total_features == 1000

    def test_percent_complete_is_fraction_of_total(self) -> None:
        # The dialog uses this for its progress bar; pinning the
        # math here means a future change to clamping or rounding
        # has to update the test alongside.
        from gratisgis_client.endpoints.import_jobs import ImportJob

        job = ImportJob.model_validate(
            {
                "id": "j",
                "itemId": "i",
                "layerId": "l",
                "status": "running",
                "mode": "replace",
                "totalFeatures": 1000,
                "processedFeatures": 250,
            }
        )
        assert job.percent_complete == 0.25

    def test_percent_complete_is_none_when_total_unknown(self) -> None:
        from gratisgis_client.endpoints.import_jobs import ImportJob

        job = ImportJob.model_validate(
            {
                "id": "j",
                "itemId": "i",
                "layerId": "l",
                "status": "running",
                "mode": "replace",
                "totalFeatures": None,
                "processedFeatures": 250,
            }
        )
        assert job.percent_complete is None

    def test_percent_complete_caps_at_one(self) -> None:
        # The worker has been observed to over-report processed by
        # a small amount on the final batch. Clamp so the progress
        # bar doesn't briefly show 110%.
        from gratisgis_client.endpoints.import_jobs import ImportJob

        job = ImportJob.model_validate(
            {
                "id": "j",
                "itemId": "i",
                "layerId": "l",
                "status": "succeeded",
                "mode": "replace",
                "totalFeatures": 1000,
                "processedFeatures": 1100,
            }
        )
        assert job.percent_complete == 1.0

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("queued", False),
            ("running", False),
            ("succeeded", True),
            ("failed", True),
            ("cancelled", True),
        ],
    )
    def test_is_terminal_flag(self, status: str, expected: bool) -> None:
        # The dialog's poll loop uses this to know when to stop
        # hitting the portal; getting it wrong means an endless
        # poll storm after the user closes the dialog.
        from gratisgis_client.endpoints.import_jobs import ImportJob

        job = ImportJob.model_validate(
            {
                "id": "j",
                "itemId": "i",
                "layerId": "l",
                "status": status,
                "mode": "replace",
            }
        )
        assert job.is_terminal is expected


class TestImportJobsCancel:
    @pytest.mark.asyncio
    async def test_cancel_posts_and_returns_updated_job(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=f"{PORTAL_URL}/api/import-jobs/job-1/cancel",
            json={
                "id": "job-1",
                "itemId": "i",
                "layerId": "l",
                "status": "cancelled",
                "mode": "replace",
            },
        )
        endpoint = ImportJobsEndpoint(http)
        job = await endpoint.cancel("job-1")
        assert job.status == "cancelled"


class TestImportJobsListActive:
    @pytest.mark.asyncio
    async def test_list_active_returns_typed_rows(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            url=f"{PORTAL_URL}/api/items/item-1/import-jobs/active",
            json=[
                {
                    "id": "j1",
                    "itemId": "item-1",
                    "layerId": "a",
                    "status": "queued",
                    "mode": "replace",
                },
                {
                    "id": "j2",
                    "itemId": "item-1",
                    "layerId": "b",
                    "status": "running",
                    "mode": "append",
                },
            ],
        )
        endpoint = ImportJobsEndpoint(http)
        jobs = await endpoint.list_active("item-1")
        assert [j.id for j in jobs] == ["j1", "j2"]
        assert [j.status for j in jobs] == ["queued", "running"]

    @pytest.mark.asyncio
    async def test_list_active_handles_non_list_response(
        self, http: PortalHttp, httpx_mock: HTTPXMock
    ) -> None:
        # Defense in depth: an upstream proxy returning an error
        # envelope (instead of the array) shouldn't blow up the
        # banner that calls this on a timer.
        httpx_mock.add_response(
            method="GET",
            url=f"{PORTAL_URL}/api/items/item-1/import-jobs/active",
            json={"error": "boom"},
        )
        endpoint = ImportJobsEndpoint(http)
        jobs = await endpoint.list_active("item-1")
        assert jobs == []
