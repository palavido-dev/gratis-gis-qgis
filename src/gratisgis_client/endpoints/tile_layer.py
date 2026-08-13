# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tile-layer finalize endpoint.

After a successful PUT of a PMTiles file to MinIO via the
presigned upload, the client calls finalize() so the portal can
read the PMTiles header, persist metadata on the item, and queue
the pyramid worker for any further processing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


class TileLayerEndpoint:
    """Wrapper over ``/items/:id/tile-layer/...``."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    def finalize(
        self,
        *,
        item_id: str,
        storage_key: str,
        storage_url: str,
        file_name: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        """Tell the portal the upload landed and to read the header.

        Returns the populated TileLayerData envelope the portal
        wrote to ``item.data``. The shape varies by tile flavor
        (PMTiles vs MBTiles vs COG) so we leave it as a dict
        rather than a typed model; the dialog mostly cares about
        the success/failure of the call, not the shape.
        """
        body = self._http.request_json(
            "POST",
            f"/items/{item_id}/tile-layer/finalize",
            json={
                "storageKey": storage_key,
                "storageUrl": storage_url,
                "fileName": file_name,
                "sizeBytes": size_bytes,
            },
        )
        return body if isinstance(body, dict) else {}

    def retry_pyramid(self, *, item_id: str) -> dict[str, Any]:
        """Retry a failed PMTiles pyramid build.

        Flips the item back to processingState='cog-ready' so the
        worker picks it up on the next tick. Useful for the QGIS
        dialog when finalize succeeds but the async pyramid pass
        later fails (the dialog can offer a Retry button).
        """
        body = self._http.request_json(
            "POST",
            f"/items/{item_id}/tile-layer/retry-pyramid",
        )
        return body if isinstance(body, dict) else {}
