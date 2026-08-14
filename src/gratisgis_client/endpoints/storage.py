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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from gratisgis_client._parse import (
    int_or,
    opt_str,
    req_bool,
    req_str,
    require_dict,
)

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


@dataclass(frozen=True, kw_only=True)
class PresignedUpload:
    """Response from POST /storage/presign-upload.

    The plugin uses ``upload_url`` to PUT bytes directly to MinIO
    (no portal round-trip in the middle); ``key`` is handed to the
    finalize step unchanged.
    """

    upload_url: str
    """Short-lived presigned PUT URL. Expires in ~60 seconds."""

    public_url: str
    """Where the object will be readable after upload. For private
    kinds (item-file, item-tile-layer, feature-attachment) this
    URL is mediated by the portal's ACL-checked proxy."""

    key: str
    """The complete storage key, ``<kind>/<uuid>``, prefix included.

    Pass it to finalize exactly as received. This docstring used to
    claim the value was the bare UUID, and the raster publish believed
    it and prepended the prefix a second time, which named an object
    that did not exist.
    """

    max_bytes: int = 0
    """Per-file ceiling the portal enforces. Callers should
    refuse uploads above this before initiating the PUT."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PresignedUpload:
        payload = require_dict(data, "PresignedUpload")
        return cls(
            upload_url=req_str(payload, "uploadUrl"),
            public_url=req_str(payload, "publicUrl"),
            key=req_str(payload, "key"),
            max_bytes=int_or(payload, "maxBytes", 0),
        )


@dataclass(frozen=True, kw_only=True)
class TileLayerSpaceCheck:
    """Response from POST /tile-layer/check-space."""

    ok: bool
    reason: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TileLayerSpaceCheck:
        payload = require_dict(data, "TileLayerSpaceCheck")
        return cls(ok=req_bool(payload, "ok"), reason=opt_str(payload, "reason"))


class StorageEndpoint:
    """Wrapper over the storage + tile-layer-specific upload helpers."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    def presign_upload(
        self,
        *,
        kind: AssetKind,
        content_type: str = "application/octet-stream",
        size_bytes: int | None = None,
    ) -> PresignedUpload:
        """Mint a short-lived presigned PUT URL.

        Pass ``size_bytes`` whenever the caller knows the file size:
        the portal enforces its per-kind size cap at presign time
        (rejecting oversized files before any bytes move) and bakes
        the length into the presigned signature, so the subsequent
        PUT must send exactly that ``Content-Length``.
        """
        payload: dict[str, Any] = {"kind": kind, "contentType": content_type}
        if size_bytes is not None:
            payload["sizeBytes"] = size_bytes
        body = self._http.request_json(
            "POST",
            "/storage/presign-upload",
            json=payload,
        )
        return PresignedUpload.from_api(body)

    def check_tile_layer_space(
        self,
        *,
        file_name: str,
        size_bytes: int,
    ) -> TileLayerSpaceCheck:
        """Pre-flight disk-space check for a tile_layer upload.

        Best-effort: if the portal returns 4xx/5xx the dialog
        should fail-open and let the real PUT surface the error.
        """
        body = self._http.request_json(
            "POST",
            "/tile-layer/check-space",
            json={"fileName": file_name, "sizeBytes": size_bytes},
        )
        return TileLayerSpaceCheck.from_api(body)
