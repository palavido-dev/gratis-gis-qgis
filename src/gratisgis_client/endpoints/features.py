# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-feature CRUD on a portal data_layer v3 item.

Wraps the routes under
``/api/items/:id/layers/:layerId/features...``:

  - POST   features            (append: bulk insert)
  - PATCH  features/:fid       (update geometry and/or properties)
  - DELETE features/:fid       (remove)

The portal returns a typed insert summary for append; PATCH returns
the updated feature row; DELETE returns 204. We stay forgiving on
the response shapes (unknown keys ignored) so a portal-side addition
doesn't break already-deployed plugins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gratisgis_client._parse import (
    int_or,
    opt_dict,
    opt_str,
    req_str,
    require_dict,
    str_list,
)

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


@dataclass(frozen=True, kw_only=True)
class FeatureIn:
    """Payload for one feature in an append.

    Matches the portal's ``AppendFeatureDto``. ``global_id`` is an
    optional client-supplied stable identifier; the engine uses it
    to dedupe re-submissions (the offline-sync path in Phase 7
    depends on this being honored).
    """

    global_id: str | None = None
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> FeatureIn:
        payload = require_dict(data, "FeatureIn")
        return cls(
            global_id=opt_str(payload, "globalId"),
            geometry=opt_dict(payload, "geometry"),
            properties=opt_dict(payload, "properties"),
        )

    def to_api_dict(self) -> dict[str, Any]:
        """The wire shape, camelCase, with unset fields omitted.

        None values are dropped rather than sent: the portal treats
        an explicit null geometry as "clear", and the dedupe path
        must not see a null globalId key.
        """
        out: dict[str, Any] = {}
        if self.global_id is not None:
            out["globalId"] = self.global_id
        if self.geometry is not None:
            out["geometry"] = self.geometry
        if self.properties is not None:
            out["properties"] = self.properties
        return out


@dataclass(frozen=True, kw_only=True)
class AppendResult:
    """Outcome of a POST features call."""

    inserted: int = 0
    """Number of feature rows written. Equal to len(features) on a
    fully-successful append; less when the engine deduped via
    globalId."""

    deduplicated: int = 0
    """Inputs the engine resolved to an existing live feature via
    globalId instead of writing a new row."""

    global_ids: list[str] = field(default_factory=list)
    """Feature ids, order-aligned with the request's features array,
    covering new and deduplicated rows alike. This is the id the
    update / delete routes address, so the push-edits flow writes it
    back into the local layer; a re-push can then update the feature
    instead of duplicating it with a second create."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AppendResult:
        payload = require_dict(data, "AppendResult")
        return cls(
            inserted=int_or(payload, "inserted", 0),
            deduplicated=int_or(payload, "deduplicated", 0),
            global_ids=str_list(payload, "globalIds"),
        )


@dataclass(frozen=True, kw_only=True)
class UpdateResult:
    """Outcome of a PATCH features/:fid call.

    The portal returns the updated feature shape, but we only model
    the fields the QGIS-side edit-commit cares about. Tests assert
    against the raw response dict for the full shape.
    """

    id: str
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> UpdateResult:
        payload = require_dict(data, "UpdateResult")
        return cls(
            id=req_str(payload, "id"),
            geometry=opt_dict(payload, "geometry"),
            properties=opt_dict(payload, "properties"),
        )


class FeaturesEndpoint:
    """Wrapper over ``/items/:id/layers/:layerId/features...``."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    def append(
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
        body = {"features": [f.to_api_dict() for f in features]}
        out = self._http.request_json(
            "POST",
            f"/items/{item_id}/layers/{layer_id}/features",
            json=body,
        )
        if isinstance(out, dict):
            return AppendResult.from_api(out)
        return AppendResult()

    def update(
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
        out = self._http.request_json(
            "PATCH",
            f"/items/{item_id}/layers/{layer_id}/features/{feature_id}",
            json=body,
        )
        return UpdateResult.from_api(out or {})

    def delete(
        self,
        *,
        item_id: str,
        layer_id: str,
        feature_id: str,
    ) -> None:
        """Soft-delete a single feature (204 No Content on success)."""
        self._http.request_json(
            "DELETE",
            f"/items/{item_id}/layers/{layer_id}/features/{feature_id}",
        )

    def download_geojson(
        self,
        *,
        item_id: str,
        layer_id: str,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> dict[str, Any]:
        """Pull a full GeoJSON FeatureCollection for a layer.

        Phase 7 (offline clone) uses this to grab the layer's
        features in one shot, then writes them locally to a
        GeoPackage. The portal endpoint streams the whole feature
        set (no pagination), so callers should be ready for a
        multi-MB response on county-scale layers.

        ``bbox`` is the standard min-lng, min-lat, max-lng, max-lat
        tuple in CRS84; the portal clips the feature set on the
        server before returning. Omit for the full layer.
        """
        params: dict[str, Any] = {}
        if bbox is not None:
            params["bbox"] = ",".join(str(x) for x in bbox)
        body = self._http.request_json(
            "GET",
            f"/items/{item_id}/layers/{layer_id}/geojson",
            params=params or None,
        )
        if not isinstance(body, dict):
            # The portal always returns a FeatureCollection on
            # success; a non-dict body means an upstream proxy
            # injected something we can't use. Return an empty
            # collection so the dialog renders "0 features" rather
            # than crashing on .get().
            return {"type": "FeatureCollection", "features": []}
        return body
