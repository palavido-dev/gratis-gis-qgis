# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ingest and import-jobs endpoint wrappers.

These pin the wire shape at the transport seam so a portal-side
field rename can't silently break the plugin's publish flow.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.import_jobs import ImportJob, ImportJobsEndpoint
from gratisgis_client.endpoints.ingest import IngestEndpoint
from gratisgis_client.http import PortalHttp
from tests.client.transport_stub import (
    FakeTransport,
    body_json,
    json_response,
    path_of,
)

PORTAL_URL = "https://portal.example"


class _FakeAuth:
    """Stand-in for AuthManager that hands out a static token.

    The real manager talks to Keycloak; for endpoint tests we just
    need request_json to attach an Authorization header.
    """

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


# Ingest endpoint


class TestIngestStage:
    def test_stage_parses_portal_envelope(self, tmp_path: Path) -> None:
        # Pinning the response shape against what the portal's
        # IngestController.stage actually returns. Adding a field
        # later is fine (unknown keys ignored); renaming one breaks
        # here and we update both sides in lockstep.
        transport = FakeTransport().add(
            json_response(
                {
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
                }
            )
        )
        gpkg = tmp_path / "parcels.gpkg"
        gpkg.write_bytes(b"fake-bytes")

        endpoint = IngestEndpoint(_http(transport))
        result = endpoint.stage(file_path=str(gpkg))

        assert path_of(transport.requests[0]) == "/api/ingest/stage"
        assert result.staging_id == "stg_abc"
        assert result.file_name == "parcels.gpkg"
        assert result.size_bytes == 1024
        assert len(result.layers) == 1
        layer = result.layers[0]
        assert layer.name == "parcels"
        assert layer.geometry_type == "polygon"
        assert layer.feature_count == 1000
        assert layer.fields[0].name == "PIN"

    def test_stage_uses_file_basename_when_name_omitted(self, tmp_path: Path) -> None:
        # The portal's `originalName` should reflect the picked file
        # (so the wizard can show "parcels.gpkg" rather than the
        # opaque stagingId in the success message).
        transport = FakeTransport().add(
            json_response({"stagingId": "x", "fileName": "parcels.gpkg", "layers": []})
        )
        gpkg = tmp_path / "parcels.gpkg"
        gpkg.write_bytes(b"x")

        endpoint = IngestEndpoint(_http(transport))
        endpoint.stage(file_path=str(gpkg))

        sent = transport.requests[0]
        # Multipart bodies contain the filename in the
        # Content-Disposition header of each part.
        assert sent.body is not None
        assert b'filename="parcels.gpkg"' in sent.body
        assert b"x" in sent.body

    def test_stage_ignores_unknown_top_level_fields(self, tmp_path: Path) -> None:
        # Forward-compat: a portal that adds a `quotaRemaining` key
        # later should not break already-deployed plugins.
        transport = FakeTransport().add(
            json_response(
                {
                    "stagingId": "s",
                    "fileName": "x",
                    "layers": [],
                    "quotaRemaining": 999,
                }
            )
        )
        gpkg = tmp_path / "x.gpkg"
        gpkg.write_bytes(b"x")
        endpoint = IngestEndpoint(_http(transport))
        result = endpoint.stage(file_path=str(gpkg))
        assert result.staging_id == "s"


class TestIngestLayerRoundTrip:
    def test_to_api_dict_matches_probe_wire_shape(self) -> None:
        # The publish wizard hands staged layers to the v3
        # layer-from-probe builder, which expects the probe response
        # keys verbatim.
        from gratisgis_client.endpoints.ingest import IngestLayer

        layer = IngestLayer.from_api(
            {
                "name": "parcels",
                "geometryType": "polygon",
                "fields": [{"name": "PIN", "type": "string"}],
                "featureCount": 5,
            }
        )
        assert layer.to_api_dict() == {
            "name": "parcels",
            "geometryType": "polygon",
            "fields": [{"name": "PIN", "type": "string"}],
            "featureCount": 5,
        }


# Import-jobs endpoint


class TestImportJobsEnqueue:
    def test_enqueue_posts_expected_body(self) -> None:
        transport = FakeTransport().add(
            json_response(
                {
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
                }
            )
        )

        endpoint = ImportJobsEndpoint(_http(transport))
        job = endpoint.enqueue(
            item_id="item-1",
            layer_id="parcels",
            staging_id="stg_x",
            source_layer_name="parcels",
            mode="replace",
        )

        # Verify the wire shape the portal expects.
        sent = transport.requests[0]
        assert path_of(sent) == "/api/items/item-1/layers/parcels/import-jobs"
        assert body_json(sent) == {
            "stagingId": "stg_x",
            "sourceLayerName": "parcels",
            "mode": "replace",
        }
        assert job.id == "job-1"
        assert job.status == "queued"
        assert job.mode == "replace"

    def test_default_mode_is_replace(self) -> None:
        # Replace is the wizard's default because it's what users
        # mean by 'publish'. Append is a power-user opt-in.
        transport = FakeTransport().add(
            json_response(
                {
                    "id": "j",
                    "itemId": "i",
                    "layerId": "l",
                    "status": "queued",
                    "mode": "replace",
                }
            )
        )
        endpoint = ImportJobsEndpoint(_http(transport))
        endpoint.enqueue(
            item_id="i",
            layer_id="l",
            staging_id="s",
            source_layer_name="src",
        )
        body = body_json(transport.requests[0])
        assert isinstance(body, dict)
        assert body["mode"] == "replace"


class TestImportJobProgress:
    def test_get_returns_typed_job_with_progress_fields(self) -> None:
        transport = FakeTransport().add(
            json_response(
                {
                    "id": "job-1",
                    "itemId": "i",
                    "layerId": "l",
                    "status": "running",
                    "mode": "replace",
                    "totalFeatures": 1000,
                    "processedFeatures": 250,
                    "insertedFeatures": 250,
                }
            )
        )
        endpoint = ImportJobsEndpoint(_http(transport))
        job = endpoint.get("job-1")
        assert path_of(transport.requests[0]) == "/api/import-jobs/job-1"
        assert job.status == "running"
        assert job.processed_features == 250
        assert job.total_features == 1000

    def test_percent_complete_is_fraction_of_total(self) -> None:
        # The dialog uses this for its progress bar; pinning the
        # math here means a future change to clamping or rounding
        # has to update the test alongside.
        job = ImportJob.from_api(
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
        job = ImportJob.from_api(
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
        job = ImportJob.from_api(
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
        job = ImportJob.from_api(
            {
                "id": "j",
                "itemId": "i",
                "layerId": "l",
                "status": status,
                "mode": "replace",
            }
        )
        assert job.is_terminal is expected

    def test_unknown_status_is_rejected(self) -> None:
        # Status drives the poll loop's exit condition; parsing an
        # unrecognized one into the model would leave the dialog
        # polling something it cannot interpret.
        with pytest.raises(ValueError):
            ImportJob.from_api(
                {
                    "id": "j",
                    "itemId": "i",
                    "layerId": "l",
                    "status": "exploded",
                    "mode": "replace",
                }
            )


class TestImportJobsCancel:
    def test_cancel_posts_and_returns_updated_job(self) -> None:
        transport = FakeTransport().add(
            json_response(
                {
                    "id": "job-1",
                    "itemId": "i",
                    "layerId": "l",
                    "status": "cancelled",
                    "mode": "replace",
                }
            )
        )
        endpoint = ImportJobsEndpoint(_http(transport))
        job = endpoint.cancel("job-1")
        assert path_of(transport.requests[0]) == "/api/import-jobs/job-1/cancel"
        assert transport.requests[0].method == "POST"
        assert job.status == "cancelled"


class TestImportJobsListActive:
    def test_list_active_returns_typed_rows(self) -> None:
        transport = FakeTransport().add(
            json_response(
                [
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
                ]
            )
        )
        endpoint = ImportJobsEndpoint(_http(transport))
        jobs = endpoint.list_active("item-1")
        assert path_of(transport.requests[0]) == "/api/items/item-1/import-jobs/active"
        assert [j.id for j in jobs] == ["j1", "j2"]
        assert [j.status for j in jobs] == ["queued", "running"]

    def test_list_active_handles_non_list_response(self) -> None:
        # Defense in depth: an upstream proxy returning an error
        # envelope (instead of the array) shouldn't blow up the
        # banner that calls this on a timer.
        transport = FakeTransport().add(json_response({"error": "boom"}))
        endpoint = ImportJobsEndpoint(_http(transport))
        jobs = endpoint.list_active("item-1")
        assert jobs == []
