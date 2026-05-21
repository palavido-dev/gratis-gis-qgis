# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-feature CRUD on a portal data_layer v3 item.

Wraps the routes under
``/api/items/:id/layers/:layerId/features...``:

  - POST   features            (append: bulk insert)
  - PATCH  features/:fid       (update geometry and/or properties)
  - DELETE features/:fid       (remove)

The portal returns a typed insert summary for append; PATCH returns
the updated feature row; DELETE returns 204. We stay forgiving on
the response shapes (``extra='ignore'``) so a portal-side addition
doesn't break already-deployed plugins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


class FeatureIn(BaseModel):
    """Payload for one feature in an append.

    Matches the portal's ``AppendFeatureDto``. ``globalId`` is an
    optional client-supplied stable identifier; the engine uses it
    to dedupe re-submissions (the offline-sync path in Phase 7
    depends on this being honored).
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    global_id: str | None = Field(default=None, alias="globalId")
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None


class AppendResult(BaseModel):
    """Outcome of a POST features call."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    inserted: int = 0
    """Number of feature rows written. Equal to len(features) on a
    fully-successful append; less when the engine deduped via
    globalId."""


class UpdateResult(BaseModel):
    """Outcome of a PATCH features/:fid call.

    The portal returns the updated feature shape, but we only model
    the fields the QGIS-side edit-commit cares about. Tests assert
    against the raw response dict for the full shape.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None


class FeaturesEndpoint:
    """Wrapper over ``/items/:id/layers/:layerId/features...``."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    async def append(
        self,
        *,
        item_id: str,
        layer_id: str,
        features: list[FeatureIn],
    ) -> AppendResult:
        """Bulk insert one or more features into the layer.

        The portal accepts up to a few thousand features in one
        call; for larger batches the caller should chunk so a
        partial failure doesn't roll back everything.
        """
        body = {
            "features": [
                f.model_dump(by_alias=True, exclude_none=True) for f in features
            ]
        }
        out = await self._http.request_json(
            "POST",
            f"/items/{item_id}/layers/{layer_id}/features",
            json=body,
        )
        if isinstance(out, dict):
            return AppendResult.model_validate(out)
        return AppendResult()

    async def update(
        self,
        *,
        item_id: str,
        layer_id: str,
        feature_id: str,
        geometry: dict[str, Any] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> UpdateResult:
        """Patch a single feature.

        Pass ``geometry`` or ``properties`` (or both). Omitting a
        field leaves it unchanged: don't send an explicit None
        unless the user actually wants to clear it (the portal will
        store the JSON null, which is rarely what you want).
        """
        body: dict[str, Any] = {}
        if geometry is not None:
            body["geometry"] = geometry
        if properties is not None:
            body["properties"] = properties
        out = await self._http.request_json(
            "PATCH",
            f"/items/{item_id}/layers/{layer_id}/features/{feature_id}",
            json=body,
        )
        return UpdateResult.model_validate(out or {})

    async def delete(
        self,
        *,
        item_id: str,
        layer_id: str,
        feature_id: str,
    ) -> None:
        """Soft-delete a single feature (204 No Content on success)."""
        await self._http.request_json(
            "DELETE",
            f"/items/{item_id}/layers/{layer_id}/features/{feature_id}",
        )
