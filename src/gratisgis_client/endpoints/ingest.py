# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest endpoint: stage a vector file for later async import.

Wraps the portal's ``POST /api/ingest/stage`` route. The route does
two things in one upload:

  1. Holds the file bytes on the portal-api server under
     ``/tmp/gg-staging/<id>/`` for up to one hour.
  2. Returns the same probe shape as ``/ingest/probe``: per-layer
     name, geometry type, fields, and feature count.

The Phase 3 publish-vector flow uses staging so the wizard does one
upload, lets the user fill in metadata, then enqueues one import
job per layer without re-uploading the bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


class IngestField(BaseModel):
    """One attribute column from a probed source layer."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    type: str
    """Portal-side normalized type: 'string' | 'number' | 'boolean' | 'date'."""


class IngestLayer(BaseModel):
    """Per-source-layer probe result.

    Mirrors what the portal returns from ``/ingest/probe`` and from
    the ``layers`` field of ``/ingest/stage``.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    """Source-layer name inside the uploaded file. Matches what the
    later import call passes as ``sourceLayerName``."""

    geometry_type: str | None = Field(default=None, alias="geometryType")
    """'point' | 'line' | 'polygon' | None. None means a non-spatial
    layer (a table) -- still importable, just no map preview."""

    fields: list[IngestField] = Field(default_factory=list)
    feature_count: int = Field(default=0, alias="featureCount")


class StageResult(BaseModel):
    """Response envelope for ``POST /api/ingest/stage``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    staging_id: str = Field(alias="stagingId")
    """Opaque id the caller hands back to the import-jobs endpoint."""

    file_name: str = Field(alias="fileName")
    """Original filename the user uploaded (for display in the wizard)."""

    size_bytes: int = Field(default=0, alias="sizeBytes")
    layers: list[IngestLayer] = Field(default_factory=list)
    expires_at: str | None = Field(default=None, alias="expiresAt")
    """ISO-8601 UTC. Helps the dialog warn before the upload silently
    falls out of /tmp/gg-staging/."""


class IngestEndpoint:
    """Wrapper over ``/api/ingest/...`` routes."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    async def stage(
        self,
        *,
        file_path: str,
        file_name: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> StageResult:
        """Upload a vector file for staged import.

        ``file_path`` is read from disk (we don't accept bytes
        directly because callers already have the file on disk; a
        bytes overload would just add a code path with no real user).
        ``file_name`` defaults to the basename of ``file_path`` so the
        portal's ``originalName`` matches what the user picked.
        """
        import os

        with open(file_path, "rb") as fh:
            blob = fh.read()
        name = file_name or os.path.basename(file_path)

        body = await self._http.request_multipart(
            "POST",
            "/ingest/stage",
            files={"file": (name, blob, content_type)},
        )
        return StageResult.model_validate(body)

    async def probe(
        self,
        *,
        file_path: str,
        file_name: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Probe-only variant. Same upload, no staging.

        Use when the caller wants schema info without committing to
        an import. Stage + ignore would also work but eats the
        portal's /tmp budget for an hour, so we expose probe as a
        first-class option for read-only inspection.
        """
        import os

        with open(file_path, "rb") as fh:
            blob = fh.read()
        name = file_name or os.path.basename(file_path)

        body = await self._http.request_multipart(
            "POST",
            "/ingest/probe",
            files={"file": (name, blob, content_type)},
        )
        if isinstance(body, dict):
            return body
        # Pathological non-dict response from a proxy; return an
        # empty probe shape so callers don't crash on .get().
        return {"layers": []}
