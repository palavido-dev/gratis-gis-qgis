# SPDX-License-Identifier: AGPL-3.0-or-later
"""Translate portal map symbology to QGIS and back (#26).

The portal styles a layer with ``MapLayerStyle`` (one block of colors
per geometry kind) plus a ``MapLayerRenderer`` (simple, one color per
value, or class breaks). QGIS styles a vector tile layer with a list
of ``QgsVectorTileBasicRendererStyle`` entries, each carrying a
geometry type, an optional filter expression, and a symbol.

Both directions live here. The mapping itself (colors, filters,
ordering) is pure and tested; the two functions that touch QGIS
(``apply_portal_style``, ``capture_layer_symbology``) only assemble
QGIS objects from the pure output.

Scope, stated rather than implied: simple, unique-values, and
class-breaks renderers, flat colors, stroke widths, point radius.
Dash patterns, icons, data-driven expressions, labels, and scaled
symbology classes pass through untouched; a layer using them opens
with its base colors, which is a recognizable map rather than a
default-styled one.
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .log import get_logger

_log = get_logger(__name__)

#: Cap on generated per-category entries. Past this a legend stops
#: being readable and QGIS slows on every repaint; the base style
#: still colors the remainder.
MAX_CATEGORIES = 60


def parse_color(value: Any) -> tuple[int, int, int, int] | None:
    """A portal color string as (r, g, b, a 0-255), or None.

    The portal writes ``#rrggbb`` from its pickers and
    ``rgba(r, g, b, a)`` where opacity sliders were involved. Both
    have to parse, because which one a map carries depends on how its
    author last touched the style.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", text)
    if match:
        raw = match.group(1)
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), 255)
    match = re.fullmatch(r"#([0-9a-fA-F]{3})", text)
    if match:
        raw = match.group(1)
        r, g, b = (int(c * 2, 16) for c in raw)
        return (r, g, b, 255)
    match = re.fullmatch(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([0-9.]+)\s*)?\)",
        text,
    )
    if match:
        r, g, b = (min(255, int(match.group(i))) for i in (1, 2, 3))
        alpha = match.group(4)
        try:
            a = round(min(1.0, max(0.0, float(alpha))) * 255) if alpha else 255
        except ValueError:
            a = 255
        return (r, g, b, a)
    return None


def color_text(rgba: tuple[int, int, int, int]) -> str:
    """(r, g, b, a) back to the portal's spelling.

    Hex when fully opaque, because that is what the portal's color
    picker writes and re-reads; rgba() only when alpha matters.
    """
    r, g, b, a = rgba
    if a >= 255:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"rgba({r}, {g}, {b}, {round(a / 255, 3)})"


@dataclass(frozen=True)
class StyleEntry:
    """One vector-tile style row, pure enough to assert on."""

    label: str
    #: "polygon" | "line" | "point"
    geometry: str
    #: QGIS expression, or "" for match-everything.
    filter: str
    fill: tuple[int, int, int, int] | None
    stroke: tuple[int, int, int, int] | None
    stroke_width: float
    point_radius: float


def _quote_value(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_field(field: str) -> str:
    return '"' + field.replace('"', '""') + '"'


def _base_colors(
    style: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    style = style if isinstance(style, dict) else {}
    point = style.get("point") if isinstance(style.get("point"), dict) else {}
    line = style.get("line") if isinstance(style.get("line"), dict) else {}
    poly = style.get("polygon") if isinstance(style.get("polygon"), dict) else {}
    return {"point": point, "line": line, "polygon": poly}


def _entry_for_geometry(
    geometry: str,
    blocks: Mapping[str, Mapping[str, Any]],
    *,
    label: str,
    filter_expr: str,
    override: tuple[int, int, int, int] | None,
) -> StyleEntry:
    """One entry, portal block colors with an optional renderer color.

    A renderer color (a category's or a class's) replaces the MAIN
    color of the geometry: fill for polygons, stroke for lines, fill
    for points. Outlines and widths stay from the base style, which is
    exactly how the portal's own canvas composes them.
    """
    point = blocks["point"]
    line = blocks["line"]
    poly = blocks["polygon"]
    if geometry == "polygon":
        fill = override or parse_color(poly.get("fillColor"))
        if fill is not None:
            opacity = poly.get("fillOpacity")
            if isinstance(opacity, (int, float)):
                fill = (*fill[:3], round(max(0.0, min(1.0, opacity)) * 255))
        return StyleEntry(
            label=label, geometry="polygon", filter=filter_expr,
            fill=fill, stroke=parse_color(poly.get("strokeColor")),
            stroke_width=_number(poly.get("strokeWidth"), 1.0),
            point_radius=0.0,
        )
    if geometry == "line":
        return StyleEntry(
            label=label, geometry="line", filter=filter_expr,
            fill=None, stroke=override or parse_color(line.get("color")),
            stroke_width=_number(line.get("width"), 1.0),
            point_radius=0.0,
        )
    return StyleEntry(
        label=label, geometry="point", filter=filter_expr,
        fill=override or parse_color(point.get("color")),
        stroke=parse_color(point.get("strokeColor")),
        stroke_width=_number(point.get("strokeWidth"), 1.0),
        point_radius=_number(point.get("radius"), 4.0),
    )


def tile_style_entries(
    style: Mapping[str, Any] | None,
    renderer: Mapping[str, Any] | None,
) -> list[StyleEntry]:
    """The full style list for one portal layer, base entries last.

    QGIS draws every matching entry, so category and class entries
    carry filters and the unfiltered base entries follow as the
    catch-all: a feature outside every category still draws in the
    layer's base colors, the same fallback the portal renders.
    """
    blocks = _base_colors(style)
    entries: list[StyleEntry] = []
    kind = renderer.get("kind") if isinstance(renderer, dict) else None

    if kind == "unique-values" and isinstance(renderer, dict):
        field = str(renderer.get("field") or "")
        categories = renderer.get("categories")
        if field and isinstance(categories, list):
            for category in categories[:MAX_CATEGORIES]:
                if not isinstance(category, dict):
                    continue
                value = str(category.get("value") or "")
                color = parse_color(category.get("color"))
                if color is None:
                    continue
                expr = f"{_quote_field(field)} = {_quote_value(value)}"
                for geometry in ("polygon", "line", "point"):
                    entries.append(
                        _entry_for_geometry(
                            geometry, blocks,
                            label=f"{value}",
                            filter_expr=expr,
                            override=color,
                        )
                    )

    if kind == "class-breaks" and isinstance(renderer, dict):
        field = str(renderer.get("field") or "")
        stops = renderer.get("stops")
        colors = renderer.get("colors")
        if (
            field
            and isinstance(stops, list)
            and isinstance(colors, list)
            and len(colors) == len(stops) + 1
            and all(isinstance(s, (int, float)) for s in stops)
        ):
            quoted = _quote_field(field)
            for index, raw_color in enumerate(colors):
                color = parse_color(raw_color)
                if color is None:
                    continue
                if index == 0:
                    expr = f"{quoted} < {stops[0]}"
                    label = f"under {stops[0]}"
                elif index == len(stops):
                    expr = f"{quoted} >= {stops[-1]}"
                    label = f"{stops[-1]} and up"
                else:
                    expr = (
                        f"{quoted} >= {stops[index - 1]} AND "
                        f"{quoted} < {stops[index]}"
                    )
                    label = f"{stops[index - 1]} to {stops[index]}"
                for geometry in ("polygon", "line", "point"):
                    entries.append(
                        _entry_for_geometry(
                            geometry, blocks,
                            label=label, filter_expr=expr, override=color,
                        )
                    )

    for geometry in ("polygon", "line", "point"):
        entries.append(
            _entry_for_geometry(
                geometry, blocks, label="", filter_expr="", override=None
            )
        )
    return entries


def _number(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# -----------------------------------------------------------
# QGIS side
# -----------------------------------------------------------


def apply_portal_style(
    layer: Any,
    style: Mapping[str, Any] | None,
    renderer: Mapping[str, Any] | None,
) -> bool:
    """Apply a portal layer's look to a QGIS vector tile layer.

    Returns False for layers this cannot style (rasters, and vector
    tile support missing from the build), which the caller reports
    rather than hides.
    """
    try:
        from qgis.core import (  # type: ignore[import-not-found]
            QgsFillSymbol,
            QgsLineSymbol,
            QgsMarkerSymbol,
            QgsVectorTileBasicRenderer,
            QgsVectorTileBasicRendererStyle,
            QgsVectorTileLayer,
        )
        from qgis.PyQt.QtGui import QColor  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - stripped build
        return False
    if not isinstance(layer, QgsVectorTileLayer):
        return False

    from qgis.core import Qgis, QgsWkbTypes  # type: ignore[import-not-found]

    from .qgis_compat import resolve_enum

    # QGIS 3.30 moved GeometryType under Qgis; older builds keep it
    # on QgsWkbTypes. Same value either way.
    scoped = getattr(Qgis, "GeometryType", None)
    geometry_types = {
        "point": resolve_enum(
            (scoped, "Point"), (QgsWkbTypes, "PointGeometry")
        ),
        "line": resolve_enum(
            (scoped, "Line"), (QgsWkbTypes, "LineGeometry")
        ),
        "polygon": resolve_enum(
            (scoped, "Polygon"), (QgsWkbTypes, "PolygonGeometry")
        ),
    }

    def qcolor(rgba: tuple[int, int, int, int]) -> Any:
        return QColor(rgba[0], rgba[1], rgba[2], rgba[3])

    styles = []
    for entry in tile_style_entries(style, renderer):
        row = QgsVectorTileBasicRendererStyle()
        row.setStyleName(entry.label or entry.geometry)
        row.setGeometryType(geometry_types[entry.geometry])
        if entry.filter:
            row.setFilterExpression(entry.filter)
        if entry.geometry == "polygon":
            symbol = QgsFillSymbol.createSimple({})
            if entry.fill is not None:
                symbol.setColor(qcolor(entry.fill))
            if entry.stroke is not None:
                first = symbol.symbolLayer(0)
                first.setStrokeColor(qcolor(entry.stroke))
                first.setStrokeWidth(entry.stroke_width * 0.26)
        elif entry.geometry == "line":
            symbol = QgsLineSymbol.createSimple({})
            if entry.stroke is not None:
                symbol.setColor(qcolor(entry.stroke))
            symbol.setWidth(entry.stroke_width * 0.26)
        else:
            symbol = QgsMarkerSymbol.createSimple({})
            if entry.fill is not None:
                symbol.setColor(qcolor(entry.fill))
            symbol.setSize(entry.point_radius * 2 * 0.26)
            if entry.stroke is not None:
                first = symbol.symbolLayer(0)
                with contextlib.suppress(AttributeError):
                    first.setStrokeColor(qcolor(entry.stroke))
        row.setSymbol(symbol)
        row.setEnabled(True)
        styles.append(row)

    tile_renderer = QgsVectorTileBasicRenderer()
    tile_renderer.setStyles(styles)
    layer.setRenderer(tile_renderer)
    layer.triggerRepaint()
    return True


def capture_layer_symbology(
    layer: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read a QGIS vector layer's renderer as portal (style, renderer).

    The publish direction. Single symbol becomes the base style,
    categorized becomes unique-values, graduated becomes class-breaks.
    Anything else (rule-based, heatmap, 2.5D) returns (None, None) and
    the published layer takes the portal's defaults, which is the
    honest outcome for renderers with no portal equivalent.
    """
    try:
        renderer = layer.renderer()
    except Exception:
        return (None, None)
    if renderer is None:
        return (None, None)
    kind = getattr(renderer, "type", lambda: "")()

    if kind == "singleSymbol":
        symbol = renderer.symbol()
        return (_style_from_symbol(layer, symbol), {"kind": "simple"})

    if kind == "categorizedSymbol":
        field = str(renderer.classAttribute() or "")
        categories = []
        base_symbol = None
        for category in renderer.categories():
            symbol = category.symbol()
            if symbol is None:
                continue
            if base_symbol is None:
                base_symbol = symbol
            rgba = _symbol_rgba(symbol)
            if rgba is None:
                continue
            categories.append(
                {"value": str(category.value()), "color": color_text(rgba)}
            )
        if not field or not categories:
            return (None, None)
        return (
            _style_from_symbol(layer, base_symbol),
            {"kind": "unique-values", "field": field, "categories": categories},
        )

    if kind == "graduatedSymbol":
        field = str(renderer.classAttribute() or "")
        ranges = list(renderer.ranges())
        if not field or not ranges:
            return (None, None)
        # Portal shape: N stops bound N+1 colors. QGIS ranges are
        # [lower, upper] pairs; interior boundaries become the stops
        # and each range contributes its color in order.
        stops = [float(r.upperValue()) for r in ranges[:-1]]
        colors = []
        for r in ranges:
            rgba = _symbol_rgba(r.symbol())
            colors.append(color_text(rgba) if rgba else "#888888")
        return (
            _style_from_symbol(layer, ranges[0].symbol()),
            {
                "kind": "class-breaks",
                "field": field,
                "stops": stops,
                "colors": colors,
            },
        )

    return (None, None)


def _symbol_rgba(symbol: Any) -> tuple[int, int, int, int] | None:
    try:
        color = symbol.color()
        return (color.red(), color.green(), color.blue(), color.alpha())
    except Exception:
        return None


def _style_from_symbol(layer: Any, symbol: Any) -> dict[str, Any] | None:
    """A portal MapLayerStyle for the layer's geometry, partial on
    purpose: only the fields QGIS actually knows are written, and the
    portal merges them over its defaults."""
    if symbol is None:
        return None
    rgba = _symbol_rgba(symbol)
    if rgba is None:
        return None
    geometry = None
    try:
        raw = layer.geometryType()
        # PyQt6 scoped enums refuse int(); their .value carries it.
        geometry = int(getattr(raw, "value", raw))
    except Exception:
        pass
    main = color_text(rgba)
    # QgsWkbTypes.GeometryType: 0 point, 1 line, 2 polygon.
    if geometry == 1:
        width = 1.0
        with contextlib.suppress(Exception):
            width = max(0.1, float(symbol.width()) / 0.26)
        return {"line": {"color": main, "width": round(width, 2)}}
    if geometry == 2:
        entry: dict[str, Any] = {
            "fillColor": color_text((*rgba[:3], 255)),
            "fillOpacity": round(rgba[3] / 255, 3),
        }
        try:
            first = symbol.symbolLayer(0)
            stroke = first.strokeColor()
            entry["strokeColor"] = color_text(
                (stroke.red(), stroke.green(), stroke.blue(), stroke.alpha())
            )
            entry["strokeWidth"] = round(
                max(0.1, float(first.strokeWidth()) / 0.26), 2
            )
        except Exception:
            _log.debug("no polygon stroke readable", exc_info=True)
        return {"polygon": entry}
    entry = {"color": main}
    with contextlib.suppress(Exception):
        entry["radius"] = round(max(0.5, float(symbol.size()) / 0.26 / 2), 2)
    return {"point": entry}
