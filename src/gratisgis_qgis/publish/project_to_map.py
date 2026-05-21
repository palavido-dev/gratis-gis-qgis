# SPDX-License-Identifier: AGPL-3.0-or-later
"""Translate QGIS project state into a portal `map` item payload.

Phase 6: "Publish current project as map". The flow:

  1. Walk QGIS's layer tree top-to-bottom.
  2. For each layer, recognize the source URI. We support:
       - OAPIF -> matches a portal data_layer item (use itemId)
       - XYZ MVT -> matches a portal tile_layer item (use itemId)
       - Anything else -> "external" layer, listed in the result
         as a `skipped` entry so the publish dialog can surface
         it to the user.
  3. Capture the canvas viewport (center + zoom).
  4. Emit a `MapData` dict matching the portal's web-map shape.

This module is pure-Python: callers (the publish dialog) pull
state from QGIS and pass it in as plain dataclasses, so the
shape-mapping logic stays testable without the QGIS runtime in
the import path.

The output dict is the `data` payload that ships in
`POST /api/items {type: 'map', data: <here>}`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..browser.uris import parse_oapif_uri, parse_vector_tile_uri


@dataclass(frozen=True)
class CanvasLayer:
    """One layer pulled from the QGIS project, normalized to the
    minimum shape the translation needs. Populated by the publish
    dialog from QGIS's layer-tree iteration; kept dataclass-shaped
    so the tests can construct it without QGIS.
    """

    name: str
    """User-visible label in the QGIS Layers panel."""

    source_uri: str
    """The raw QGIS provider URI (e.g. OAPIF, XYZ template)."""

    provider: str
    """The QGIS provider key (e.g. 'OAPIF', 'vectortile', 'wms')."""

    visible: bool
    """Layer-tree visibility checkbox state."""

    opacity: float = 1.0
    """0..1 layer opacity. Layer-tree opacity, not feature-level."""


@dataclass(frozen=True)
class CanvasViewport:
    """Camera state at publish time. Coordinates in CRS84 (lon/lat)
    so the dialog reprojects from whatever CRS the canvas was on.
    """

    center_lng: float
    center_lat: float
    zoom: float


@dataclass(frozen=True)
class ProjectSnapshot:
    """The slice of QGIS project state we translate. The publish
    dialog populates this from `QgsProject.instance()` + the
    active map canvas.
    """

    title: str
    layers: list[CanvasLayer]
    viewport: CanvasViewport


@dataclass(frozen=True)
class MapTranslation:
    """Output: the portal-shaped `map` payload + a per-layer
    audit so the dialog can show what was kept vs. skipped.
    """

    data: dict[str, Any]
    """The `data` envelope for POST /api/items."""

    skipped: list["SkippedLayer"] = field(default_factory=list)
    """Layers the translator couldn't map to a portal item id.
    The dialog should list these to the user; publishing
    proceeds without them.
    """


@dataclass(frozen=True)
class SkippedLayer:
    name: str
    provider: str
    reason: str


# Match identifiers used on the portal-side MapLayerSource union.
_SourceKind = Literal["data-layer", "arcgis-rest"]


def translate(snapshot: ProjectSnapshot) -> MapTranslation:
    """Translate a `ProjectSnapshot` into a portal map payload.

    Layers without a recognized portal source go into `skipped`
    rather than blocking the publish; the caller decides whether
    to warn or proceed.
    """
    map_layers: list[dict[str, Any]] = []
    skipped: list[SkippedLayer] = []
    # Top-of-list = bottom-of-canvas; QGIS draws bottom-up. The
    # portal's MapData.layers is the same order convention, so we
    # pass the list through as-is. The dialog hands us the
    # snapshot in canvas order; reversal happens there if needed.
    for lyr in snapshot.layers:
        resolved = _resolve_layer(lyr)
        if resolved is None:
            skipped.append(
                SkippedLayer(
                    name=lyr.name,
                    provider=lyr.provider,
                    reason=_skip_reason(lyr),
                )
            )
            continue
        item_id, kind = resolved
        map_layers.append(
            {
                "id": f"qgis-{lyr.name}",
                "title": lyr.name,
                "visible": lyr.visible,
                "opacity": _clamp_unit(lyr.opacity),
                "source": _source_block(kind, item_id),
            }
        )
    data: dict[str, Any] = {
        "version": 1,
        "basemap": "",
        "layers": map_layers,
        "view": {
            "center": [snapshot.viewport.center_lng, snapshot.viewport.center_lat],
            "zoom": snapshot.viewport.zoom,
        },
    }
    return MapTranslation(data=data, skipped=skipped)


# -----------------------------------------------------------
# Internal recognizer
# -----------------------------------------------------------


def _resolve_layer(layer: CanvasLayer) -> tuple[str, _SourceKind] | None:
    """Inverse of the URI builders. Returns (item_id, source_kind)
    when the layer is one we can put on the portal map, or None
    when it's an unknown provider / off-portal source.
    """
    if layer.provider.upper() == "OAPIF":
        parsed = parse_oapif_uri(layer.source_uri)
        if parsed is not None:
            _, item_id = parsed
            return item_id, "data-layer"
        return None
    if layer.provider.lower() == "vectortile":
        parsed = parse_vector_tile_uri(layer.source_uri)
        if parsed is not None:
            _, item_id = parsed
            # Tile-layer sources project onto data-layer on the
            # portal side too: a tile-served layer is just a
            # data_layer rendered through the Tiles endpoint.
            # When the portal eventually surfaces an explicit
            # `tile-layer` source kind we can split this branch.
            return item_id, "data-layer"
        return None
    return None


def _source_block(kind: _SourceKind, item_id: str) -> dict[str, Any]:
    """Compose a MapLayerSource block matching the portal's union."""
    if kind == "data-layer":
        return {"kind": "data-layer", "itemId": item_id}
    if kind == "arcgis-rest":
        # Reserved for a future "publish-with-external-services"
        # path; emitting the discriminant + url here keeps the
        # shape future-proof even though the recognizer doesn't
        # yet produce it.
        return {"kind": "arcgis-rest", "url": item_id}
    raise AssertionError(f"unsupported kind {kind!r}")


def _skip_reason(layer: CanvasLayer) -> str:
    p = layer.provider.lower()
    if p in ("oapif", "vectortile"):
        return (
            "Source URL doesn't match a recognized portal endpoint. "
            "Sign in to the matching connection or use a published "
            "layer from the GratisGIS Browser tree."
        )
    if p in ("wms", "wmts", "wfs", "arcgisfeatureserver", "arcgismapserver"):
        return (
            f"{layer.provider} layer points at an external service. "
            "Add it as an arcgis_service / wms_service item in the "
            "portal first, then re-publish."
        )
    if p in ("ogr", "memory", "delimitedtext", "gpx", "spatialite"):
        return (
            "Local file or in-memory layer. Publish the data to a "
            "portal data_layer (Phase 3) before including it in a map."
        )
    return f"Unsupported provider: {layer.provider}"


def _clamp_unit(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)
