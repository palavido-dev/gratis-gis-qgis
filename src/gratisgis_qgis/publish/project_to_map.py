# SPDX-License-Identifier: AGPL-3.0-or-later
"""Translate QGIS project state into a portal `map` item payload.

Phase 6: "Publish current project as map". The flow:

  1. Walk QGIS's layer tree top-to-bottom.
  2. For each layer, recognize the source URI. We support:
       - OAPIF -> matches a portal data_layer item (use itemId)
       - XYZ MVT (vectortile / xyzvectortiles) -> ditto, the
         per-layer collection id encodes itemId + layerKey
       - arcgismapserver / arcgisfeatureserver -> matches a
         portal connected-service item via URL lookup (uses
         arcgis-rest source with sourceItemId back-ref)
       - wms (XYZ-mode raster) -> matches a portal basemap item
         via tileUrl lookup; sets MapData.basemap rather than
         adding the layer to layers[]
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

import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import unquote

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

    skipped: list[SkippedLayer] = field(default_factory=list)
    """Layers the translator couldn't map to a portal item id.
    The dialog should list these to the user; publishing
    proceeds without them.
    """


@dataclass(frozen=True)
class SkippedLayer:
    name: str
    provider: str
    reason: str


@dataclass(frozen=True)
class PortalServiceRef:
    """A portal connected-service item (arcgis_service / service)
    keyed by its upstream MapServer / FeatureServer URL.
    """

    item_id: str
    service_type: Literal["MapServer", "FeatureServer"]


@dataclass(frozen=True)
class PortalIndex:
    """Lookup the publish flow uses to recognize layers that came
    from the portal even when the QGIS layer URI points at the
    EXTERNAL service URL (not at a gratis-gis.org endpoint).

    The dialog pre-fetches the portal's items list and builds the
    two dicts below before calling ``translate``. Empty / None
    indexes are valid; ``translate`` falls back to "skipped" for
    layers that need a lookup but get no match.
    """

    basemaps_by_tile_url: dict[str, str] = field(default_factory=dict)
    """Mapping of basemap ``data.tileUrl`` -> basemap item id."""

    services_by_url: dict[str, PortalServiceRef] = field(default_factory=dict)
    """Mapping of connected-service ``data.url`` (root URL, no
    layer suffix) -> service item ref. Used to backref ArcGIS REST
    layers that came from a portal service item."""


# Match identifiers used on the portal-side MapLayerSource union.
_SourceKind = Literal["data-layer", "arcgis-rest"]


def translate(
    snapshot: ProjectSnapshot,
    portal_index: PortalIndex | None = None,
) -> MapTranslation:
    """Translate a `ProjectSnapshot` into a portal map payload.

    Layers without a recognized portal source go into `skipped`
    rather than blocking the publish; the caller decides whether
    to warn or proceed.

    ``portal_index`` enables matching ArcGIS REST and WMS-XYZ
    basemap layers back to the portal items they came from. When
    omitted, those layers fall through to the skipped list.
    """
    index = portal_index or PortalIndex()
    map_layers: list[dict[str, Any]] = []
    skipped: list[SkippedLayer] = []
    basemap_item_id = ""
    # Top-of-list = bottom-of-canvas; QGIS draws bottom-up. The
    # portal's MapData.layers is the same order convention, so we
    # pass the list through as-is. The dialog hands us the
    # snapshot in canvas order; reversal happens there if needed.
    for lyr in snapshot.layers:
        resolved = _resolve_layer(lyr, index)
        if resolved is None:
            skipped.append(
                SkippedLayer(
                    name=lyr.name,
                    provider=lyr.provider,
                    reason=_skip_reason(lyr),
                )
            )
            continue
        if isinstance(resolved, _ResolvedBasemap):
            # Only one basemap can render on a portal map -- the
            # first matched wins; any extras get dropped silently
            # since they'd be invisible anyway.
            if basemap_item_id == "":
                basemap_item_id = resolved.item_id
            continue
        if isinstance(resolved, _ResolvedDataLayer):
            map_layers.append(
                _emit_layer(
                    lyr,
                    source={
                        "kind": "data-layer",
                        "itemId": resolved.item_id,
                        **(
                            {"layerKey": resolved.layer_key}
                            if resolved.layer_key
                            else {}
                        ),
                    },
                )
            )
            continue
        if isinstance(resolved, _ResolvedArcgisRest):
            source: dict[str, Any] = {
                "kind": "arcgis-rest",
                "url": resolved.url,
                "layerId": resolved.layer_id,
                "serviceType": resolved.service_type,
            }
            if resolved.source_item_id:
                source["sourceItemId"] = resolved.source_item_id
            map_layers.append(_emit_layer(lyr, source=source))
            continue
        # _resolve_layer returned an unknown type -- shouldn't
        # happen, but skip rather than crash if it does.
        skipped.append(
            SkippedLayer(
                name=lyr.name,
                provider=lyr.provider,
                reason="Unrecognized resolution shape (plugin bug).",
            )
        )
    data: dict[str, Any] = {
        "version": 1,
        "basemap": basemap_item_id,
        "layers": map_layers,
        "view": {
            "center": [snapshot.viewport.center_lng, snapshot.viewport.center_lat],
            "zoom": snapshot.viewport.zoom,
        },
    }
    return MapTranslation(data=data, skipped=skipped)


def _emit_layer(lyr: CanvasLayer, *, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"qgis-{lyr.name}",
        "title": lyr.name,
        "visible": lyr.visible,
        "opacity": _clamp_unit(lyr.opacity),
        "source": source,
    }


# -----------------------------------------------------------
# Internal recognizer
# -----------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedDataLayer:
    item_id: str
    layer_key: str | None


@dataclass(frozen=True)
class _ResolvedArcgisRest:
    url: str
    layer_id: int
    service_type: Literal["MapServer", "FeatureServer"]
    source_item_id: str | None


@dataclass(frozen=True)
class _ResolvedBasemap:
    item_id: str


_Resolved = _ResolvedDataLayer | _ResolvedArcgisRest | _ResolvedBasemap


def _resolve_layer(
    layer: CanvasLayer, index: PortalIndex
) -> _Resolved | None:
    """Inverse of the URI builders. Returns a typed resolution when
    the layer maps to a portal item; None when off-portal.

    ``index`` lets the recognizer match external service URLs back
    to the portal item they came from (basemap items by tileUrl,
    connected-service items by service-root URL).
    """
    provider = layer.provider.lower()

    # --- OGC API Features (data_layer items) ---
    if provider == "oapif":
        parsed = parse_oapif_uri(layer.source_uri)
        if parsed is None:
            return None
        _, collection_id = parsed
        item_id, layer_key = _split_collection_id(collection_id)
        return _ResolvedDataLayer(item_id=item_id, layer_key=layer_key)

    # --- Vector tile (data_layer items rendered as MVT) ---
    # QGIS reports `vectortile` (older) or `xyzvectortiles` (XYZ
    # template mode, what our plugin emits). Treat both as our
    # data_layer URI shape.
    if provider in ("vectortile", "xyzvectortiles"):
        parsed = parse_vector_tile_uri(layer.source_uri)
        if parsed is None:
            return None
        _, collection_id = parsed
        item_id, layer_key = _split_collection_id(collection_id)
        return _ResolvedDataLayer(item_id=item_id, layer_key=layer_key)

    # --- ArcGIS REST services (connected-service items) ---
    if provider in ("arcgismapserver", "arcgisfeatureserver"):
        return _resolve_arcgis(layer, index)

    # --- WMS provider in XYZ raster mode (basemap items) ---
    if provider == "wms":
        return _resolve_wms_basemap(layer, index)

    return None


def _split_collection_id(collection_id: str) -> tuple[str, str | None]:
    """Split ``<itemId>__<layerKey>`` collection ids into the two
    components the portal MapLayerSource needs. Bare-UUID ids
    (single-layer items) come back with ``layer_key = None``.
    """
    if "__" in collection_id:
        item_id, layer_key = collection_id.split("__", 1)
        return item_id, layer_key
    return collection_id, None


_QUOTED_KV_RE = re.compile(r"(\w+)='([^']*)'")


def _parse_quoted_kv_uri(uri: str) -> dict[str, str]:
    """Parse a single-quoted ``key='value' key='value'`` URI into
    a dict. Used by the arcgismapserver / arcgisfeatureserver
    handlers.
    """
    return {m.group(1): m.group(2) for m in _QUOTED_KV_RE.finditer(uri)}


def _resolve_arcgis(layer: CanvasLayer, index: PortalIndex) -> _Resolved | None:
    """Map an ArcGIS REST layer (FeatureServer/N or MapServer
    rendered with ``layers='show:N'``) back to a portal connected
    -service item plus a typed source block.
    """
    kv = _parse_quoted_kv_uri(layer.source_uri)
    url = kv.get("url", "").rstrip("/")
    if not url:
        return None
    provider = layer.provider.lower()
    if provider == "arcgisfeatureserver":
        # FeatureServer URI is `url='<root>/<N>'`. Strip the
        # trailing /N to get the service root, parse N as layer id.
        m = re.search(r"/(\d+)$", url)
        if m is None:
            # Without a layer index we can't represent the layer
            # in the portal arcgis-rest source. Skip.
            return None
        layer_id = int(m.group(1))
        service_root = url[: m.start()]
        service_type: Literal["MapServer", "FeatureServer"] = "FeatureServer"
    else:
        # MapServer URI is `url='<root>' layers='show:N'`. The
        # root is already root; layer id comes from `layers`.
        layers_kv = kv.get("layers", "")
        m = re.match(r"^show:(\d+)$", layers_kv)
        if m is None:
            return None
        layer_id = int(m.group(1))
        service_root = url
        service_type = "MapServer"
    source_item_id = None
    ref = index.services_by_url.get(service_root)
    if ref is not None and ref.service_type == service_type:
        source_item_id = ref.item_id
    return _ResolvedArcgisRest(
        url=service_root,
        layer_id=layer_id,
        service_type=service_type,
        source_item_id=source_item_id,
    )


def _resolve_wms_basemap(layer: CanvasLayer, index: PortalIndex) -> _Resolved | None:
    """Match a WMS XYZ-mode layer back to a portal basemap item by
    its tileUrl. WMS layers that aren't in XYZ mode, or whose URL
    doesn't match any portal basemap, fall through to skipped.
    """
    uri = layer.source_uri
    # XYZ shape: `type=xyz&url=<url>&zmin=...&zmax=...`. The URL
    # is URL-encoded inside the URI. Pull it out and decode.
    if "type=xyz" not in uri or "url=" not in uri:
        return None
    # Match `url=...` up to the next `&` (or end of string).
    m = re.search(r"url=([^&]+)", uri)
    if m is None:
        return None
    tile_url = unquote(m.group(1))
    item_id = index.basemaps_by_tile_url.get(tile_url)
    if item_id is None:
        return None
    return _ResolvedBasemap(item_id=item_id)


def _skip_reason(layer: CanvasLayer) -> str:
    p = layer.provider.lower()
    if p in ("oapif", "vectortile", "xyzvectortiles"):
        return (
            "Source URL doesn't match a recognized portal endpoint. "
            "Sign in to the matching connection or use a published "
            "layer from the GratisGIS Browser tree."
        )
    if p in ("wms", "wmts", "wfs", "arcgisfeatureserver", "arcgismapserver"):
        return (
            f"{layer.provider} layer points at an external service "
            "without a matching portal item. Add it as a service / "
            "basemap item in the portal first, then re-publish."
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
