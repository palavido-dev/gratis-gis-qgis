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

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gratisgis_client._parse import (
    int_or,
    opt_str,
    req_str,
    require_dict,
)

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


@dataclass(frozen=True, kw_only=True)
class IngestField:
    """One attribute column from a probed source layer."""

    name: str
    type: str
    """Portal-side normalized type: 'string' | 'number' | 'boolean' | 'date'."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> IngestField:
        payload = require_dict(data, "IngestField")
        return cls(name=req_str(payload, "name"), type=req_str(payload, "type"))


@dataclass(frozen=True, kw_only=True)
class IngestLayer:
    """Per-source-layer probe result.

    Mirrors what the portal returns from ``/ingest/probe`` and from
    the ``layers`` field of ``/ingest/stage``.
    """

    name: str
    """Source-layer name inside the uploaded file. Matches what the
    later import call passes as ``sourceLayerName``."""

    geometry_type: str | None = None
    """'point' | 'line' | 'polygon' | None. None means a non-spatial
    layer (a table), still importable, just no map preview."""

    fields: list[IngestField] = field(default_factory=list)
    feature_count: int = 0

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> IngestLayer:
        payload = require_dict(data, "IngestLayer")
        rows = payload.get("fields") or []
        if not isinstance(rows, list):
            raise ValueError("field 'fields': expected a list")
        return cls(
            name=req_str(payload, "name"),
            geometry_type=opt_str(payload, "geometryType"),
            fields=[IngestField.from_api(row) for row in rows],
            feature_count=int_or(payload, "featureCount", 0),
        )

    def to_api_dict(self) -> dict[str, Any]:
        """The probe wire shape back, camelCase keys.

        The publish wizard feeds this straight into the v3
        layer-from-probe builder, which expects the same keys the
        portal's probe response uses.
        """
        return {
            "name": self.name,
            "geometryType": self.geometry_type,
            "fields": [{"name": f.name, "type": f.type} for f in self.fields],
            "featureCount": self.feature_count,
        }


@dataclass(frozen=True, kw_only=True)
class StageResult:
    """Response envelope for ``POST /api/ingest/stage``."""

    staging_id: str
    """Opaque id the caller hands back to the import-jobs endpoint."""

    file_name: str
    """Original filename the user uploaded (for display in the wizard)."""

    size_bytes: int = 0
    layers: list[IngestLayer] = field(default_factory=list)
    expires_at: str | None = None
    """ISO-8601 UTC. Helps the dialog warn before the upload silently
    falls out of /tmp/gg-staging/."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> StageResult:
        payload = require_dict(data, "StageResult")
        rows = payload.get("layers") or []
        if not isinstance(rows, list):
            raise ValueError("field 'layers': expected a list")
        return cls(
            staging_id=req_str(payload, "stagingId"),
            file_name=req_str(payload, "fileName"),
            size_bytes=int_or(payload, "sizeBytes", 0),
            layers=[IngestLayer.from_api(row) for row in rows],
            expires_at=opt_str(payload, "expiresAt"),
        )


class IngestEndpoint:
    """Wrapper over ``/api/ingest/...`` routes."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    def stage(
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
        with open(file_path, "rb") as fh:
            blob = fh.read()
        name = file_name or os.path.basename(file_path)

        body = self._http.request_multipart(
            "POST",
            "/ingest/stage",
            files={"file": (name, blob, content_type)},
        )
        return StageResult.from_api(body)

    def probe(
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
        with open(file_path, "rb") as fh:
            blob = fh.read()
        name = file_name or os.path.basename(file_path)

        body = self._http.request_multipart(
            "POST",
            "/ingest/probe",
            files={"file": (name, blob, content_type)},
        )
        if isinstance(body, dict):
            return body
        # Pathological non-dict response from a proxy; return an
        # empty probe shape so callers don't crash on .get().
        return {"layers": []}
