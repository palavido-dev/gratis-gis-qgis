# SPDX-License-Identifier: AGPL-3.0-or-later
"""Give portal layers the extent their data actually occupies.

A tiled layer reports the whole world as its extent. That is not a bug
in QGIS: a tile pyramid is defined over the whole world regardless of
where its content sits, and the provider has no cheap way to find out
which tiles are empty. The visible cost is that "Zoom to Layer" on a
portal layer zooms out to the entire planet, which is useless for a
county parcel set and actively confusing for a layer someone just
published from three polygons.

The portal knows the real extent, so the fix is to carry it, and every
part of how is forced rather than chosen:

- Applying it after the layer exists, not through the URI, because
  probing real QGIS found no provider parameter that sets a tiled
  layer's extent while ``setExtent()`` after construction sticks.
- Doing it on a project signal, not at each call site, because a layer
  dragged out of the Browser tree is built by QGIS from a mime URI with
  no plugin code involved. ``layerWasAdded`` is the one place that sees
  the tree, the search dock and a reopened project alike.
- Two carriers, URI and custom property, because neither covers both
  cases on its own. See ``EXTENT_PROPERTY``.
"""
from __future__ import annotations

import contextlib
from typing import Any

from .browser.uris import format_extent, parse_extent, parse_extent_suffix
from .log import get_logger

_log = get_logger(__name__)

#: Where the extent is kept once the layer exists.
#:
#: The URI gets the layer's first extent, because when a layer is
#: dragged out of the Browser tree QGIS builds it from a mime URI and
#: no plugin code runs. But the URI is not durable: saving a project
#: rewrites a raster layer's source through the provider's own encoder,
#: which drops parameters it does not recognise, so a reopened project
#: has lost it. (Vector-tile sources keep theirs, which is exactly why
#: this only showed up once rasters were tested separately.)
#:
#: Custom properties are QGIS's own mechanism for this and survive the
#: round trip intact, already populated by the time the layer is added.
#: So the URI seeds it and the property preserves it.
EXTENT_PROPERTY = "gratisgis/extent"

#: Half-width applied to a degenerate axis, in EPSG:4326 degrees.
#: A single-point layer (one feature, or several stacked) has a
#: zero-width bbox, and zooming to a zero-width rectangle leaves QGIS
#: at an arbitrary scale. Roughly 110 m, which frames a point sensibly
#: without pretending to know the feature's real size.
_DEGENERATE_PAD_DEGREES = 0.001


def apply_recorded_extent(layer: Any) -> bool:
    """Set ``layer``'s extent from the bbox recorded on it.

    Returns True when an extent was applied. False covers every
    ordinary case: a layer that is not ours, one published before the
    portal knew its extent, a malformed value. This runs for every
    layer added to the project, including layers with nothing to do
    with the portal, so it must be quiet and cheap on the way out.
    """
    bbox = _recorded_bbox(layer)
    if bbox is None:
        return False

    try:
        rect = _rect_in_layer_crs(layer, bbox)
        if rect is None:
            return False
        layer.setExtent(rect)
    except Exception:
        # Never let a cosmetic convenience break adding a layer.
        _log.debug("could not apply recorded extent", exc_info=True)
        return False
    _log.debug("applied recorded extent to %s", layer.name())
    return True


def _recorded_bbox(layer: Any) -> tuple[float, float, float, float] | None:
    """Find this layer's recorded extent, and make it durable.

    The custom property is consulted first because it is the one that
    survives a project round trip. Falling back to the URI covers the
    layer's first appearance, and promoting that value to the property
    is what makes the next save keep it.
    """
    try:
        stored = layer.customProperty(EXTENT_PROPERTY)
    except Exception:
        stored = None
    if isinstance(stored, str) and stored:
        bbox = parse_extent(stored)
        if bbox is not None:
            return bbox

    try:
        source = layer.source()
    except Exception:
        return None
    if not isinstance(source, str):
        return None
    bbox = parse_extent_suffix(source)
    if bbox is None:
        return None

    try:
        layer.setCustomProperty(EXTENT_PROPERTY, format_extent(bbox))
    except Exception:
        # Losing persistence is survivable; the extent still applies now.
        _log.debug("could not persist the recorded extent", exc_info=True)
    return bbox


def _rect_in_layer_crs(layer: Any, bbox: tuple[float, float, float, float]) -> Any:
    """Transform the recorded EPSG:4326 bbox into the layer's own CRS."""
    from qgis.core import (  # type: ignore[import-not-found]
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
        QgsRectangle,
    )

    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lon - min_lon <= 0:
        min_lon -= _DEGENERATE_PAD_DEGREES
        max_lon += _DEGENERATE_PAD_DEGREES
    if max_lat - min_lat <= 0:
        min_lat -= _DEGENERATE_PAD_DEGREES
        max_lat += _DEGENERATE_PAD_DEGREES

    source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    target_crs = layer.crs()
    rect = QgsRectangle(min_lon, min_lat, max_lon, max_lat)
    if not target_crs.isValid() or target_crs == source_crs:
        return rect
    transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
    return transform.transformBoundingBox(rect)


class ExtentApplier:
    """Applies recorded extents to layers as they enter the project.

    Held by the plugin for the session and disconnected on unload,
    because a plugin reload would otherwise leave the previous
    instance's slot connected to a module that no longer exists.

    One signal is enough, but only because the extent is stored in a
    custom property: those are already populated by the time
    ``layerWasAdded`` fires, including on a project reload. Verified,
    rather than assumed, since the reload path is exactly where the
    first attempt at this quietly failed for raster layers.
    """

    def __init__(self) -> None:
        self._connected = False

    def install(self) -> None:
        from qgis.core import QgsProject  # type: ignore[import-not-found]

        if self._connected:
            return
        project = QgsProject.instance()
        project.layerWasAdded.connect(self._on_layer_added)
        self._connected = True

    def remove(self) -> None:
        from qgis.core import QgsProject  # type: ignore[import-not-found]

        if not self._connected:
            return
        # Already disconnected, or the project object is gone.
        with contextlib.suppress(TypeError, RuntimeError):
            QgsProject.instance().layerWasAdded.disconnect(self._on_layer_added)
        self._connected = False

    def _on_layer_added(self, layer: Any) -> None:
        apply_recorded_extent(layer)
