# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage endpoint: presigned uploads for thumbnails, files, tile layers.

The portal mints short-lived presigned-PUT URLs against MinIO so
clients can upload bytes directly without buffering through the
API. The plugin uses this for:

  - tile_layer publish: PUT a .pmtiles file to MinIO, then
    POST /items/:id/tile-layer/finalize
  - file-item create: PUT bytes for an item-file then PATCH the
    item's data envelope with {storageKey, storageUrl, fileName}
  - item thumbnails: server-baked SVG today; this endpoint stays
    for completeness should we move to client-baked thumbs later
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


AssetKind = Literal[
    "item-thumb",
    "group-thumb",
    "user-avatar",
    "org-hero",
    "feature-attachment",
    "item-file",
    "item-tile-layer",
]


class PresignedUpload(BaseModel):
    """Response from POST /storage/presign-upload.

    The plugin uses ``upload_url`` to PUT bytes directly to MinIO
    (no portal round-trip in the middle); ``key`` is the bare UUID
    that gets handed to the finalize step.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    upload_url: str = Field(alias="uploadUrl")
    """Short-lived presigned PUT URL. Expires in ~60 seconds."""

    public_url: str = Field(alias="publicUrl")
    """Where the object will be readable after upload. For private
    kinds (item-file, item-tile-layer, feature-attachment) this
    URL is mediated by the portal's ACL-checked proxy."""

    key: str
    """The bare UUID portion of the storage key. The full key is
    ``<kind>/<key>``."""

    max_bytes: int = Field(default=0, alias="maxBytes")
    """Per-file ceiling the portal enforces. Callers should
    refuse uploads above this before initiating the PUT."""


class TileLayerSpaceCheck(BaseModel):
    """Response from POST /tile-layer/check-space."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ok: bool
    reason: str | None = None


class StorageEndpoint:
    """Wrapper over the storage + tile-layer-specific upload helpers."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    async def presign_upload(
        self,
        *,
        kind: AssetKind,
        content_type: str = "application/octet-stream",
    ) -> PresignedUpload:
        """Mint a short-lived presigned PUT URL."""
        body = await self._http.request_json(
            "POST",
            "/storage/presign-upload",
            json={"kind": kind, "contentType": content_type},
        )
        return PresignedUpload.model_validate(body)

    async def check_tile_layer_space(
        self,
        *,
        file_name: str,
        size_bytes: int,
    ) -> TileLayerSpaceCheck:
        """Pre-flight disk-space check for a tile_layer upload.

        Best-effort: if the portal returns 4xx/5xx the dialog
        should fail-open and let the real PUT surface the error.
        """
        body = await self._http.request_json(
            "POST",
            "/tile-layer/check-space",
            json={"fileName": file_name, "sizeBytes": size_bytes},
        )
        return TileLayerSpaceCheck.model_validate(body)
