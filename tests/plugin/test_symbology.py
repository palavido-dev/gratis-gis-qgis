# SPDX-License-Identifier: AGPL-3.0-or-later
"""Portal symbology to QGIS style entries, and QGIS renderers back.

The QGIS-object half runs in the smoke test against real bindings;
what is asserted here is the mapping itself: which colors land where,
which filters get generated, and the shape of what publish writes,
because a wrong filter or a swapped color draws a WRONG map rather
than an error.
"""
from __future__ import annotations

from typing import Any

from gratisgis_qgis.symbology import (
    MAX_CATEGORIES,
    StyleEntry,
    color_text,
    parse_color,
    tile_style_entries,
)


class TestParseColor:
    def test_the_two_spellings_the_portal_writes(self) -> None:
        assert parse_color("#5c6b58") == (0x5C, 0x6B, 0x58, 255)
        assert parse_color("rgba(214, 83, 126, 0.5)") == (214, 83, 126, 128)
        assert parse_color("rgb(1, 2, 3)") == (1, 2, 3, 255)
        assert parse_color("#abc") == (0xAA, 0xBB, 0xCC, 255)

    def test_garbage_is_none_not_black(self) -> None:
        """None lets the caller keep QGIS defaults; black would paint
        a confident wrong answer."""
        for bad in ("", "red", "#12345", "rgba(1,2)", None, 7):
            assert parse_color(bad) is None

    def test_color_text_round_trips(self) -> None:
        assert color_text((92, 107, 88, 255)) == "#5c6b58"
        assert parse_color(color_text((214, 83, 126, 128))) == (214, 83, 126, 128)


def _style() -> dict[str, Any]:
    return {
        "point": {"color": "#d4537e", "radius": 6,
                  "strokeColor": "#ffffff", "strokeWidth": 1},
        "line": {"color": "#185fa5", "width": 2},
        "polygon": {"fillColor": "#639922", "fillOpacity": 0.4,
                    "strokeColor": "#27500a", "strokeWidth": 1.5},
    }


class TestSimpleEntries:
    def test_one_entry_per_geometry_with_the_base_colors(self) -> None:
        entries = tile_style_entries(_style(), {"kind": "simple"})
        assert all(isinstance(e, StyleEntry) for e in entries)
        by_geometry = {e.geometry: e for e in entries}
        assert set(by_geometry) == {"polygon", "line", "point"}
        assert by_geometry["line"].stroke == (0x18, 0x5F, 0xA5, 255)
        assert by_geometry["point"].fill == (0xD4, 0x53, 0x7E, 255)
        assert by_geometry["point"].point_radius == 6.0

    def test_fill_opacity_lands_in_the_alpha_channel(self) -> None:
        entries = tile_style_entries(_style(), None)
        polygon = next(e for e in entries if e.geometry == "polygon")
        assert polygon.fill is not None
        assert polygon.fill[3] == round(0.4 * 255)

    def test_no_style_still_yields_base_entries(self) -> None:
        """A layer without style must draw in defaults, not vanish."""
        entries = tile_style_entries(None, None)
        assert {e.geometry for e in entries} == {"polygon", "line", "point"}
        assert all(e.filter == "" for e in entries)


class TestUniqueValues:
    def _renderer(self) -> dict[str, Any]:
        return {
            "kind": "unique-values",
            "field": "class",
            "categories": [
                {"value": "residential", "color": "#7f77dd"},
                {"value": "it's odd", "color": "#1d9e75"},
            ],
        }

    def test_each_category_filters_on_its_value(self) -> None:
        entries = tile_style_entries(_style(), self._renderer())
        filters = {e.filter for e in entries if e.filter}
        assert '"class" = \'residential\'' in filters
        assert '"class" = \'it\'\'s odd\'' in filters, (
            "a quote in a value must be escaped, not a broken expression"
        )

    def test_the_category_color_replaces_the_main_color_only(self) -> None:
        """Fill for polygons, stroke for lines; outlines stay from the
        base style, matching how the portal canvas composes them."""
        entries = tile_style_entries(_style(), self._renderer())
        cat_polygon = next(
            e for e in entries if e.geometry == "polygon" and "residential" in e.filter
        )
        assert cat_polygon.fill is not None
        assert cat_polygon.fill[:3] == (0x7F, 0x77, 0xDD)
        assert cat_polygon.stroke == (0x27, 0x50, 0x0A, 255)
        cat_line = next(
            e for e in entries if e.geometry == "line" and "residential" in e.filter
        )
        assert cat_line.stroke is not None
        assert cat_line.stroke[:3] == (0x7F, 0x77, 0xDD)

    def test_the_catch_all_base_entries_come_last(self) -> None:
        """QGIS draws every matching entry; the unfiltered base rows
        are the portal's own fallback for uncategorised features."""
        entries = tile_style_entries(_style(), self._renderer())
        assert [e.filter for e in entries[-3:]] == ["", "", ""]

    def test_category_count_is_capped(self) -> None:
        renderer = {
            "kind": "unique-values",
            "field": "f",
            "categories": [
                {"value": str(i), "color": "#111111"} for i in range(500)
            ],
        }
        entries = tile_style_entries(None, renderer)
        filtered = [e for e in entries if e.filter]
        assert len(filtered) == MAX_CATEGORIES * 3


class TestClassBreaks:
    def test_stops_become_range_filters_with_open_ends(self) -> None:
        renderer = {
            "kind": "class-breaks",
            "field": "acres",
            "stops": [10, 100],
            "colors": ["#111111", "#222222", "#333333"],
        }
        entries = tile_style_entries(_style(), renderer)
        filters = [e.filter for e in entries if e.geometry == "polygon" and e.filter]
        assert filters == [
            '"acres" < 10',
            '"acres" >= 10 AND "acres" < 100',
            '"acres" >= 100',
        ]

    def test_a_malformed_break_set_falls_back_to_base_styling(self) -> None:
        """colors must be exactly stops+1; anything else is a schema
        violation better drawn plainly than guessed at."""
        renderer = {
            "kind": "class-breaks", "field": "acres",
            "stops": [10], "colors": ["#111111"],
        }
        entries = tile_style_entries(_style(), renderer)
        assert all(e.filter == "" for e in entries)
