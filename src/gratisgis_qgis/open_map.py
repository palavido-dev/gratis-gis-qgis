# SPDX-License-Identifier: AGPL-3.0-or-later
"""Open a portal map as a ready QGIS layer stack (#23).

The inverse of publish-as-map. A portal map is a JSON document naming
layers by reference (data_layer sublayers, tile_layer rasters, ArcGIS
REST services), a basemap by UUID, and a camera. Opening one means
resolving every reference into a QGIS layer URI, stacking them in the
map's order inside a group named after the map, and pointing the
canvas at the map's view.

Split the way every risky surface in this plugin is split: planning is
pure and tested (``plan_map_open`` takes the map's JSON plus a dict of
prefetched referenced items and returns a plan or skip reasons, no Qt,
no network), and the QGIS half (``open_map_in_project``) only carries
the plan out. Fetches happen in a background task before planning, so
the GUI thread never waits on the portal.

What stays out, and is said to the user rather than silently dropped:
GeoJSON-by-URL sources (QGIS would read them through GDAL's /vsicurl,
the exact mechanism behind the #24 project-load deadlock), inline
GeoJSON (data lives in the map document, no stable source to point
at), live PostGIS and point-cloud sources, and non-spatial tables.
Every skip carries a plain-English reason shown after the open.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .browser.uris import (
    authed_oapif_uri,
    authed_vector_tile_uri,
    oapif_uri,
    tile_layer_xyz_uri,
    vector_tile_uri,
)
from .log import get_logger
from .publish.project_to_map import (
    MAX_ZOOM,
    MERCATOR_MAX_LAT,
    MIN_ZOOM,
    WEB_MERCATOR_SCALE_Z0,
)

_log = get_logger(__name__)


@dataclass(frozen=True)
class PlannedLayer:
    """One QGIS layer the plan wants built, top-of-stack first."""

    title: str
    #: QGIS provider key: vectortile, wms (XYZ rasters), or the two
    #: arcgis providers.
    provider: str
    uri: str
    visible: bool
    #: 0-1; applied to the whole QGIS layer.
    opacity: float
    #: Portal MapLayerStyle dict, or None to leave QGIS defaults.
    style: dict[str, Any] | None
    #: Portal MapLayerRenderer dict; None reads as simple.
    renderer: dict[str, Any] | None
    #: Name of the portal group this layer sits under, or "".
    group: str = ""


@dataclass(frozen=True)
class SkippedMapLayer:
    title: str
    reason: str


@dataclass(frozen=True)
class MapOpenPlan:
    title: str
    layers: list[PlannedLayer]
    skipped: list[SkippedMapLayer]
    #: (lon, lat, scale denominator) for the canvas, or None when the
    #: map carries no usable camera.
    view: tuple[float, float, float] | None
    #: Bottom-of-stack basemap, when the referenced item resolves to
    #: something QGIS can draw.
    basemap: PlannedLayer | None


def scale_for_zoom(zoom: float, latitude_deg: float) -> float:
    """Invert ``zoom_for_scale``: web zoom to a QGIS scale denominator.

    The cosine corrects for web mercator's latitude stretch, the same
    correction publish applies in the other direction, so a map
    published from QGIS and reopened lands on the view it left.
    """
    z = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
    lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, latitude_deg))
    return WEB_MERCATOR_SCALE_Z0 * math.cos(math.radians(lat)) / (2.0**z)


def plan_map_open(
    map_title: str,
    map_data: Mapping[str, Any],
    referenced: Mapping[str, Mapping[str, Any] | None],
    *,
    portal_url: str,
    layer_authcfg_id: str,
) -> MapOpenPlan:
    """Turn a map document into buildable layers plus honest skips.

    ``referenced`` maps every item UUID the document names to its
    fetched full item, or None when the fetch failed (deleted item,
    no access). Prefetched by the caller so this stays testable and
    the fetches stay on a worker.
    """
    raw_layers = map_data.get("layers")
    layers_in = raw_layers if isinstance(raw_layers, list) else []

    # Group titles keyed by MapLayer id, so members can name their
    # group. The portal models groups as sibling layers with a
    # group-kind source; QGIS models them as tree nodes.
    group_titles: dict[str, str] = {}
    for entry in layers_in:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if isinstance(source, dict) and source.get("kind") == "group":
            group_titles[str(entry.get("id") or "")] = str(
                entry.get("title") or "Group"
            )

    planned: list[PlannedLayer] = []
    skipped: list[SkippedMapLayer] = []
    for entry in layers_in:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        if source.get("kind") == "group":
            continue  # rendered as a QGIS group node, not a layer
        title = str(entry.get("title") or "Layer")
        group = group_titles.get(str(entry.get("groupId") or ""), "")
        outcome = _plan_one(
            title,
            entry,
            source,
            referenced,
            portal_url=portal_url,
            layer_authcfg_id=layer_authcfg_id,
        )
        if isinstance(outcome, SkippedMapLayer):
            skipped.append(outcome)
        elif outcome is not None:
            planned.append(
                PlannedLayer(
                    title=outcome.title,
                    provider=outcome.provider,
                    uri=outcome.uri,
                    visible=outcome.visible,
                    opacity=outcome.opacity,
                    style=outcome.style,
                    renderer=outcome.renderer,
                    group=group,
                )
            )

    basemap = _plan_basemap(map_data, referenced)

    view: tuple[float, float, float] | None = None
    center = map_data.get("center")
    zoom = map_data.get("zoom")
    if (
        isinstance(center, list)
        and len(center) == 2
        and all(isinstance(c, (int, float)) for c in center)
        and isinstance(zoom, (int, float))
    ):
        lon, lat = float(center[0]), float(center[1])
        view = (lon, lat, scale_for_zoom(float(zoom), lat))

    return MapOpenPlan(
        title=map_title, layers=planned, skipped=skipped, view=view,
        basemap=basemap,
    )


def _plan_one(
    title: str,
    entry: Mapping[str, Any],
    source: Mapping[str, Any],
    referenced: Mapping[str, Mapping[str, Any] | None],
    *,
    portal_url: str,
    layer_authcfg_id: str,
) -> PlannedLayer | SkippedMapLayer | None:
    kind = source.get("kind")
    visible = bool(entry.get("visible", True))
    opacity = _unit(entry.get("opacity", 1.0))
    style = entry.get("style") if isinstance(entry.get("style"), dict) else None
    renderer = (
        entry.get("renderer") if isinstance(entry.get("renderer"), dict) else None
    )

    def planned(provider: str, uri: str) -> PlannedLayer:
        return PlannedLayer(
            title=title, provider=provider, uri=uri, visible=visible,
            opacity=opacity, style=style, renderer=renderer,
        )

    if kind == "data-layer":
        item_id = str(source.get("itemId") or "")
        item = referenced.get(item_id)
        if not item:
            return SkippedMapLayer(
                title,
                "This layer's dataset is not reachable. It may have been "
                "deleted, or your account may not have access to it.",
            )
        provider, uri = _data_layer_source(
            item_id,
            str(source.get("layerKey") or "") or None,
            item,
            portal_url=portal_url,
            layer_authcfg_id=layer_authcfg_id,
        )
        return planned(provider, uri)

    if kind == "tile":
        item_id = str(source.get("itemId") or "")
        item = referenced.get(item_id)
        if not item:
            return SkippedMapLayer(
                title,
                "This layer's image is not reachable. It may have been "
                "deleted, or your account may not have access to it.",
            )
        access = str(item.get("access") or "")
        authcfg = "" if access == "public" else layer_authcfg_id
        return planned(
            "wms", tile_layer_xyz_uri(portal_url, item_id, authcfg_id=authcfg)
        )

    if kind == "arcgis-rest":
        url = str(source.get("url") or "").rstrip("/")
        layer_id = source.get("layerId")
        if not url or not isinstance(layer_id, int):
            return SkippedMapLayer(title, "This service reference is incomplete.")
        if source.get("proxyUrl"):
            # The upstream needs a credential the portal holds
            # server-side. QGIS would talk to the upstream directly
            # and be refused, which reads as a broken layer.
            return SkippedMapLayer(
                title,
                "This service needs a sign-in that only the portal holds. "
                "Open it in the portal's map viewer instead.",
            )
        if source.get("serviceType") == "FeatureServer":
            return planned(
                "arcgisfeatureserver",
                f"crs='EPSG:4326' url='{url}/{layer_id}'",
            )
        return planned(
            "arcgismapserver",
            f"crs='EPSG:3857' format='PNG32' layer='{layer_id}' url='{url}'",
        )

    if kind == "geojson-url":
        # QGIS reads remote GeoJSON through GDAL's /vsicurl, which is
        # the mechanism that deadlocks project load (#24). Not worth
        # a layer that freezes the next reopen.
        return SkippedMapLayer(
            title,
            "Web GeoJSON layers are not supported in QGIS yet.",
        )
    if kind == "geojson-inline":
        return SkippedMapLayer(
            title,
            "This layer's data lives inside the map itself and has no "
            "source QGIS can connect to.",
        )
    if kind == "postgis-live":
        return SkippedMapLayer(
            title,
            "Live database layers connect with a credential only the "
            "portal holds.",
        )
    if kind == "point-cloud":
        return SkippedMapLayer(
            title, "Point cloud layers are not supported in QGIS yet."
        )
    return SkippedMapLayer(title, "This layer kind is not supported in QGIS.")


def _data_layer_source(
    item_id: str,
    layer_key: str | None,
    item: Mapping[str, Any],
    *,
    portal_url: str,
    layer_authcfg_id: str,
) -> tuple[str, str]:
    """(provider, uri) for a map's data-layer source.

    The same defaults the Browser tree applies. Small layers (and
    tables, which cannot render as MVT at all) become TRUE feature
    layers through OGC API Features, so a map's layers arrive with
    working attribute tables; layers over the feature-default
    threshold, or with no featureCount to judge by, stay on vector
    tiles, which are the only rendering that survives WV-Parcels
    scale. Public items ride the public surfaces so the project keeps
    working for anonymous viewers; everything else the signed-in
    routes with the connection's layer key.
    """
    from .browser.items import prefers_features

    data = item.get("data")
    layers = (
        data.get("layers")
        if isinstance(data, dict) and data.get("version") == 3
        else None
    )
    matched: Mapping[str, Any] | None = None
    if isinstance(layers, list):
        for lyr in layers:
            if not isinstance(lyr, dict):
                continue
            if layer_key is None or str(lyr.get("id")) == layer_key:
                matched = lyr
                break
    feature_count: int | None = None
    if matched is not None:
        geometry = matched.get("geometryType")
        has_geometry = isinstance(geometry, str) and bool(geometry)
        raw_count = matched.get("featureCount")
        if isinstance(raw_count, int) and not isinstance(raw_count, bool):
            feature_count = raw_count
        layer_id = str(matched.get("id") or "")
        collection_id = f"{item_id}__{layer_id}" if layer_id else item_id
    else:
        # v1/v2 item: single implicit spatial layer, bare-UUID alias,
        # no per-layer count to judge by, so it stays on tiles.
        has_geometry = True
        layer_id = ""
        collection_id = item_id

    is_public = str(item.get("access") or "") == "public"
    if prefers_features(has_geometry, feature_count):
        if not is_public and layer_authcfg_id:
            return "OAPIF", authed_oapif_uri(
                portal_url, collection_id, authcfg_id=layer_authcfg_id
            )
        return "OAPIF", oapif_uri(portal_url, collection_id)

    bbox = _bbox_of(item)
    if is_public:
        return "vectortile", vector_tile_uri(
            portal_url, collection_id, extent=bbox
        )
    if layer_authcfg_id and layer_id:
        return "vectortile", authed_vector_tile_uri(
            portal_url, item_id, layer_id,
            authcfg_id=layer_authcfg_id, extent=bbox,
        )
    return "vectortile", vector_tile_uri(
        portal_url, collection_id, extent=bbox
    )


def _plan_basemap(
    map_data: Mapping[str, Any],
    referenced: Mapping[str, Mapping[str, Any] | None],
) -> PlannedLayer | None:
    """The map's basemap as a bottom-of-stack XYZ layer, if drawable.

    Mirrors the Browser's BasemapItem: only tile-URL basemaps have a
    QGIS representation today. A style-URL basemap is a MapLibre
    style document, which QGIS cannot apply to a raster layer, so the
    map opens without it rather than with a broken layer.
    """
    basemap_id = str(map_data.get("basemap") or "")
    item = referenced.get(basemap_id) if basemap_id else None
    if not item:
        return None
    data = item.get("data")
    tile_url = str(data.get("tileUrl") or "") if isinstance(data, dict) else ""
    if not tile_url:
        return None
    uri = f"type=xyz&url={quote(tile_url, safe='')}&zmin=0&zmax=22"
    return PlannedLayer(
        title=str(item.get("title") or "Basemap"),
        provider="wms", uri=uri, visible=True, opacity=1.0,
        style=None, renderer=None,
    )


def referenced_item_ids(map_data: Mapping[str, Any]) -> list[str]:
    """Every item UUID the plan will need fetched, deduplicated.

    The caller fetches these on a worker before planning. Includes the
    basemap; excludes kinds the planner will skip anyway, so a map
    full of unsupported layers costs no fetches.
    """
    ids: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "")
        if text and text not in ids:
            ids.append(text)

    add(map_data.get("basemap"))
    raw_layers = map_data.get("layers")
    for entry in raw_layers if isinstance(raw_layers, list) else []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        if source.get("kind") in ("data-layer", "tile"):
            add(source.get("itemId"))
    return ids


def _unit(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _bbox_of(item: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    bbox = item.get("bbox")
    if (
        isinstance(bbox, (list, tuple))
        and len(bbox) == 4
        and all(isinstance(v, (int, float)) for v in bbox)
    ):
        return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    return None


# -----------------------------------------------------------
# QGIS side: carry a plan out on the GUI thread
# -----------------------------------------------------------


def open_map_in_project(plan: MapOpenPlan, iface: Any) -> tuple[int, list[str]]:
    """Build the plan's layers into the current project.

    Returns (layers added, problem lines). Runs on the GUI thread;
    everything slow already happened in the fetch task. Layer
    construction is lazy in QGIS, so nothing here waits on tiles.
    """
    from qgis.core import (  # type: ignore[import-not-found]
        QgsProject,
        QgsRasterLayer,
        QgsVectorTileLayer,
    )

    problems = [f"{s.title}: {s.reason}" for s in plan.skipped]
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.insertGroup(0, plan.title)
    subgroups: dict[str, Any] = {}

    def build(planned: PlannedLayer) -> Any:
        if planned.provider == "vectortile":
            return QgsVectorTileLayer(planned.uri, planned.title)
        if planned.provider in ("wms", "arcgismapserver"):
            return QgsRasterLayer(planned.uri, planned.title, planned.provider)
        if planned.provider in ("arcgisfeatureserver", "OAPIF"):
            from qgis.core import QgsVectorLayer  # type: ignore[import-not-found]

            return QgsVectorLayer(planned.uri, planned.title, planned.provider)
        return None

    added = 0
    to_build = list(plan.layers) + ([plan.basemap] if plan.basemap else [])
    for planned in to_build:
        layer = build(planned)
        if layer is None or not layer.isValid():
            problems.append(
                f"{planned.title}: QGIS could not open this layer."
            )
            _log.warning(
                "open map: %s invalid (provider %s, uri %.120s)",
                planned.title, planned.provider, planned.uri,
            )
            continue
        if planned.style or planned.renderer:
            try:
                from .symbology import apply_portal_style

                apply_portal_style(layer, planned.style, planned.renderer)
            except Exception:
                _log.exception("style application failed for %s", planned.title)
                problems.append(
                    f"{planned.title}: opened, but its portal styling could "
                    "not be applied."
                )
        try:
            layer.setOpacity(planned.opacity)
        except Exception:  # pragma: no cover - older API
            _log.debug("no setOpacity on %s", planned.title)
        project.addMapLayer(layer, False)
        parent = group
        if planned.group:
            if planned.group not in subgroups:
                subgroups[planned.group] = group.addGroup(planned.group)
            parent = subgroups[planned.group]
        node = parent.addLayer(layer)
        if node is not None and not planned.visible:
            node.setItemVisibilityChecked(False)
        added += 1

    if plan.view is not None and iface is not None:
        _point_canvas_when_settled(iface, plan.view)
    return added, problems


def _point_canvas_when_settled(
    iface: Any, view: tuple[float, float, float]
) -> None:
    """Queue the camera set behind the layer-tree bridge's.

    Adding the first layer to an empty project makes QGIS's canvas
    bridge auto-zoom to that layer's full extent through a DEFERRED
    call, and a vector tile layer's full extent is the whole world.
    Setting the camera synchronously here therefore looked applied and
    was then thrown away a moment later, which read as "portal maps
    always open at world extent". A zero-delay timer queues our set
    after the bridge's (both are queued; the bridge scheduled first),
    so the saved viewport is what survives. Deferring also means the
    bridge has already stamped the project CRS from that first layer,
    so the transform in ``_point_canvas`` targets the real canvas CRS
    rather than an empty project's default.
    """
    from qgis.PyQt.QtCore import QTimer  # type: ignore[import-not-found]

    def apply() -> None:
        try:
            _point_canvas(iface, view)
        except Exception:  # pragma: no cover - canvas quirks
            _log.exception("could not set the canvas view")

    QTimer.singleShot(0, apply)


def _point_canvas(iface: Any, view: tuple[float, float, float]) -> None:
    """Center the canvas on the map's camera at its scale."""
    from qgis.core import (  # type: ignore[import-not-found]
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsPointXY,
        QgsProject,
    )

    lon, lat, scale = view
    canvas = iface.mapCanvas()
    # The canvas's own CRS, not a project snapshot: on a fresh project
    # the CRS was just set by the first layer added above.
    dest = canvas.mapSettings().destinationCrs()
    transform = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        dest,
        QgsProject.instance(),
    )
    canvas.setCenter(transform.transform(QgsPointXY(lon, lat)))
    canvas.zoomScale(scale)
    canvas.refresh()


def launch_open_map(profile: Any, item_id: str, title: str, iface: Any) -> None:
    """Fetch, plan, and open in one background-then-GUI flow.

    The fetches (the map item plus everything it references) run in a
    task; the plan is computed there too, since it is pure. Only the
    layer construction lands back on the GUI thread.
    """
    from qgis.PyQt.QtWidgets import QMessageBox  # type: ignore[import-not-found]

    from .portal import get_item
    from .tasks import run_in_task

    def fetch(_handle: Any) -> MapOpenPlan:
        full = get_item(profile, item_id) or {}
        data = full.get("data")
        map_data: Mapping[str, Any] = data if isinstance(data, dict) else {}
        referenced = {
            ref_id: get_item(profile, ref_id)
            for ref_id in referenced_item_ids(map_data)
        }
        return plan_map_open(
            title,
            map_data,
            referenced,
            portal_url=profile.portal_url,
            layer_authcfg_id=profile.layer_authcfg_id,
        )

    def done(plan: MapOpenPlan) -> None:
        added, problems = open_map_in_project(plan, iface)
        if problems:
            QMessageBox.information(
                iface.mainWindow() if iface else None,
                "Map opened with notes",
                f"Added {added} layer(s) from {plan.title!r}.\n\n"
                "Some layers could not be added:\n\n- "
                + "\n- ".join(problems),
            )

    def failed(exc: BaseException) -> None:
        _log.error("open map failed", exc_info=exc)
        QMessageBox.warning(
            iface.mainWindow() if iface else None,
            "Could not open map",
            f"The map could not be opened.\n\n{exc}",
        )

    run_in_task(f"GratisGIS: open map {title}", fetch, done, failed)
