# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import-jobs endpoint: async per-layer ingest with polling.

After the wizard stages a file (see ``ingest.py``) and creates an
empty v3 data_layer item, it enqueues one job per layer here. The
worker picks them up in ~1s and streams progress into the row that
this endpoint polls.

The portal exposes:

  - POST /api/items/:id/layers/:layerId/import-jobs (enqueue)
  - GET  /api/items/:id/import-jobs/active           (banner state)
  - GET  /api/import-jobs/:jobId                     (single poll)
  - POST /api/import-jobs/:jobId/cancel              (user cancel)

The portal's ``IngestController`` also exposes a legacy NDJSON
streaming endpoint at ``POST /api/items/:id/layers/:layerId/import``.
We deliberately do not wrap it: the import-jobs path is the
recommended one and uses the COPY bulk-write path (5-10x faster for
county-scale data).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


ImportJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ImportMode = Literal["replace", "append"]


class ImportJob(BaseModel):
    """One row from the portal's ``ImportJob`` table.

    The portal's ``toWire`` shapes this; we stay forgiving by
    ignoring unknown fields so a future portal-side addition
    doesn't break already-deployed plugins.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    item_id: str = Field(alias="itemId")
    layer_id: str = Field(alias="layerId")
    status: ImportJobStatus
    mode: ImportMode
    source_layer_name: str | None = Field(default=None, alias="sourceLayerName")
    source_file_name: str | None = Field(default=None, alias="sourceFileName")

    total_features: int | None = Field(default=None, alias="totalFeatures")
    """Best-effort upper bound; set by the probe step. The detail-
    page banner uses it to render percentage. Missing means
    indeterminate (the dialog should show a spinner without %)."""

    processed_features: int = Field(default=0, alias="processedFeatures")
    inserted_features: int = Field(default=0, alias="insertedFeatures")
    replaced_features: int = Field(default=0, alias="replacedFeatures")

    error_message: str | None = Field(default=None, alias="errorMessage")
    started_at: str | None = Field(default=None, alias="startedAt")
    finished_at: str | None = Field(default=None, alias="finishedAt")

    @property
    def is_terminal(self) -> bool:
        """True once the job won't change state anymore.

        Poll loops should exit as soon as this flips so we don't
        keep polling forever after a cancel/fail.
        """
        return self.status in ("succeeded", "failed", "cancelled")

    @property
    def percent_complete(self) -> float | None:
        """Fraction in [0, 1] when we have a total, else None.

        Capped at 1.0 because the worker has been observed to
        over-report processed by a small amount on the final batch
        (cosmetic; the inserted count is authoritative).
        """
        if self.total_features is None or self.total_features <= 0:
            return None
        ratio = self.processed_features / self.total_features
        if ratio < 0.0:
            return 0.0
        if ratio > 1.0:
            return 1.0
        return ratio


class ImportJobsEndpoint:
    """Wrapper over the async import-jobs surface."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    async def enqueue(
        self,
        *,
        item_id: str,
        layer_id: str,
        staging_id: str,
        source_layer_name: str,
        mode: ImportMode = "replace",
    ) -> ImportJob:
        """Enqueue a per-layer ingest job. Returns immediately."""
        body = await self._http.request_json(
            "POST",
            f"/items/{item_id}/layers/{layer_id}/import-jobs",
            json={
                "stagingId": staging_id,
                "sourceLayerName": source_layer_name,
                "mode": mode,
            },
        )
        return ImportJob.model_validate(body)

    async def get(self, job_id: str) -> ImportJob:
        """Single-job poll. Used by the wizard's progress dialog."""
        body = await self._http.request_json("GET", f"/import-jobs/{job_id}")
        return ImportJob.model_validate(body)

    async def list_active(self, item_id: str) -> list[ImportJob]:
        """All queued + running jobs for an item.

        Useful when the dialog is reopened against an item that has
        a job already in flight (e.g. a publish that the user closed
        the dialog on but didn't cancel).
        """
        body = await self._http.request_json(
            "GET", f"/items/{item_id}/import-jobs/active"
        )
        if not isinstance(body, list):
            return []
        return [ImportJob.model_validate(row) for row in body]

    async def cancel(self, job_id: str) -> ImportJob:
        """User-initiated cancel. Idempotent on terminal states."""
        body = await self._http.request_json(
            "POST", f"/import-jobs/{job_id}/cancel"
        )
        return ImportJob.model_validate(body)
