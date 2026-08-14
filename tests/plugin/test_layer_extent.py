# SPDX-License-Identifier: AGPL-3.0-or-later
"""Carrying a portal item's extent into the layer QGIS builds.

A tiled layer reports the whole world as its extent, so "Zoom to
Layer" on any portal layer zoomed out to the planet. The extent rides
along in the layer URI because nothing downstream of the Browser tree
gets handed the portal item.

These cover the pure halves: stamping the value into a URI and reading
it back. Whether QGIS honors setExtent() is a question about the real
bindings, so it is asserted in scripts/qgis_smoke.py instead, where
the answer means something.
"""
from __future__ import annotations

import pytest

from gratisgis_qgis.browser.uris import (
    authed_vector_tile_uri,
    extent_suffix,
    format_extent,
    parse_extent_suffix,
    parse_portal_layer_source,
    tile_layer_xyz_uri,
    vector_tile_uri,
)
from gratisgis_qgis.layer_extent import EXTENT_PROPERTY, _recorded_bbox

_PORTAL = "https://portal.example"
# Randolph County parcels, as the portal actually reports it.
_BBOX = (-79.8817405459576, 38.8075562828525, -79.72808554075921, 38.91672787868328)


class _FakeLayer:
    """A layer's custom-property store and source string.

    Enough for the carrier logic, which is the part that decides where
    an extent comes from and whether it will survive a save.
    """

    def __init__(self, source: str = "", properties: dict[str, object] | None = None):
        self._source = source
        self.properties: dict[str, object] = dict(properties or {})

    def source(self) -> str:  # QGIS API name
        return self._source

    def customProperty(self, key: str, default: object = None) -> object:  # QGIS API
        return self.properties.get(key, default)

    def setCustomProperty(self, key: str, value: object) -> None:  # QGIS API
        self.properties[key] = value


class TestRecordedBbox:
    """Where an extent is read from, and what gets made durable.

    The URI is the only channel available when QGIS builds a layer
    itself from a dragged mime URI, but it does not survive a project
    save for raster layers, whose source is rewritten by the provider's
    own encoder. The custom property is what persists, so a value found
    in the URI has to be promoted into one.
    """

    def test_reads_the_extent_out_of_the_uri(self) -> None:
        layer = _FakeLayer(source=vector_tile_uri(_PORTAL, "item-1", extent=_BBOX))
        assert _recorded_bbox(layer) == _BBOX

    def test_a_uri_extent_is_promoted_to_a_custom_property(self) -> None:
        # Without this the extent is correct for one session and gone
        # the next time the project is opened.
        layer = _FakeLayer(source=vector_tile_uri(_PORTAL, "item-1", extent=_BBOX))
        _recorded_bbox(layer)
        assert layer.properties[EXTENT_PROPERTY] == format_extent(_BBOX)

    def test_the_property_is_used_when_the_uri_has_lost_it(self) -> None:
        # A reopened project: the raster's source came back without the
        # parameter, so only the property can answer.
        layer = _FakeLayer(
            source=tile_layer_xyz_uri(_PORTAL, "item-1"),
            properties={EXTENT_PROPERTY: format_extent(_BBOX)},
        )
        assert _recorded_bbox(layer) == _BBOX

    def test_the_property_wins_over_the_uri(self) -> None:
        other = (-100.0, 30.0, -99.0, 31.0)
        layer = _FakeLayer(
            source=vector_tile_uri(_PORTAL, "item-1", extent=_BBOX),
            properties={EXTENT_PROPERTY: format_extent(other)},
        )
        assert _recorded_bbox(layer) == other

    def test_a_corrupt_property_falls_back_to_the_uri(self) -> None:
        layer = _FakeLayer(
            source=vector_tile_uri(_PORTAL, "item-1", extent=_BBOX),
            properties={EXTENT_PROPERTY: "garbage"},
        )
        assert _recorded_bbox(layer) == _BBOX

    def test_an_unrelated_layer_yields_nothing_and_is_left_alone(self) -> None:
        # This runs over every layer in the project, most of which have
        # nothing to do with the portal.
        layer = _FakeLayer(source="/home/matt/roads.shp")
        assert _recorded_bbox(layer) is None
        assert layer.properties == {}


class TestExtentSuffix:
    def test_absent_bbox_adds_nothing(self) -> None:
        assert extent_suffix(None) == ""

    def test_round_trips_at_full_precision(self) -> None:
        # Rounding is visible at street zoom, so the check is exact
        # equality rather than approximate.
        assert parse_extent_suffix(extent_suffix(_BBOX)) == _BBOX

    @pytest.mark.parametrize(
        "source",
        [
            "",
            "type=xyz&url=https://portal.example/x/{z}/{y}/{x}",
            "&ggextent=",
            "&ggextent=1,2,3",
            "&ggextent=1,2,3,4,5",
            "&ggextent=a,b,c,d",
            "&ggextent=nan,2,3,4",
            "&ggextent=inf,2,3,4",
            # Inverted: a min above its max is not an extent.
            "&ggextent=10,2,3,4",
            "&ggextent=1,10,3,4",
        ],
    )
    def test_unusable_values_read_as_absent(self, source: str) -> None:
        assert parse_extent_suffix(source) is None

    def test_a_later_parameter_does_not_get_swallowed(self) -> None:
        uri = f"type=xyz&url=http://x{extent_suffix(_BBOX)}&authcfg=abc123"
        assert parse_extent_suffix(uri) == _BBOX


class TestBuildersCarryTheExtent:
    """Every builder for a layer type that reports a world extent."""

    @pytest.mark.parametrize(
        "uri",
        [
            vector_tile_uri(_PORTAL, "item-1__parcels", extent=_BBOX),
            authed_vector_tile_uri(
                _PORTAL, "item-1", "parcels", authcfg_id="abc123", extent=_BBOX
            ),
            tile_layer_xyz_uri(_PORTAL, "item-1", extent=_BBOX),
        ],
    )
    def test_extent_survives_into_the_uri(self, uri: str) -> None:
        assert parse_extent_suffix(uri) == _BBOX

    @pytest.mark.parametrize(
        "uri",
        [
            vector_tile_uri(_PORTAL, "item-1__parcels"),
            authed_vector_tile_uri(_PORTAL, "item-1", "parcels", authcfg_id="abc123"),
            tile_layer_xyz_uri(_PORTAL, "item-1"),
        ],
    )
    def test_no_extent_leaves_the_uri_as_it_was(self, uri: str) -> None:
        assert "ggextent" not in uri

    def test_the_authcfg_still_parses_with_an_extent_appended(self) -> None:
        # The extent is appended after authcfg, so a naive reader that
        # took the rest of the string would swallow it and every
        # private layer would fail to authenticate.
        uri = authed_vector_tile_uri(
            _PORTAL, "item-1", "parcels", authcfg_id="abc123", extent=_BBOX
        )
        assert "&authcfg=abc123&" in uri

    @pytest.mark.parametrize(
        "uri",
        [
            vector_tile_uri(_PORTAL, "item-1__parcels", extent=_BBOX),
            authed_vector_tile_uri(
                _PORTAL, "item-1", "parcels", authcfg_id="abc123", extent=_BBOX
            ),
        ],
    )
    def test_portal_resolution_is_unaffected(self, uri: str) -> None:
        # The dialogs resolve a layer back to its portal item from this
        # same string; an extra parameter must not disturb that.
        ref = parse_portal_layer_source(uri)
        assert ref is not None
        assert ref.item_id == "item-1"
        assert ref.layer_id == "parcels"
