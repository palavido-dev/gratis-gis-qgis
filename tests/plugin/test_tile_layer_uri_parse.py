# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recognising a portal raster already on the canvas.

Publish-as-map understood four providers and none of them covered a
tile_layer item, so a portal raster on the canvas was reported as an
unpublishable local file. Both raster shapes have to be recognised,
because which one a layer gets depends on how the item was stored,
which the user cannot see.
"""
from __future__ import annotations

import pytest

from gratisgis_qgis.browser.uris import (
    parse_tile_layer_uri,
    tile_layer_cog_uri,
    tile_layer_xyz_uri,
)

_PORTAL = "https://gratisgis.org"
_ITEM = "bf03e789-a5c1-403c-b307-ea3252935cd8"


class TestRoundTripsWithTheBuilders:
    """Parse what we ourselves emit, which is what the canvas holds."""

    def test_the_cog_shape(self) -> None:
        uri = tile_layer_cog_uri(_PORTAL, _ITEM)
        assert parse_tile_layer_uri(uri) == (_PORTAL, _ITEM)

    def test_the_xyz_shape(self) -> None:
        uri = tile_layer_xyz_uri(_PORTAL, _ITEM)
        assert parse_tile_layer_uri(uri) == (_PORTAL, _ITEM)

    def test_the_xyz_shape_with_an_authcfg(self) -> None:
        # Private and org rasters carry a credential; the item id must
        # still come back.
        uri = tile_layer_xyz_uri(_PORTAL, _ITEM, authcfg_id="abc123")
        assert parse_tile_layer_uri(uri) == (_PORTAL, _ITEM)

    def test_the_xyz_shape_with_a_recorded_extent(self) -> None:
        uri = tile_layer_xyz_uri(
            _PORTAL, _ITEM, extent=(-80.0, 38.0, -79.0, 39.0)
        )
        assert parse_tile_layer_uri(uri) == (_PORTAL, _ITEM)


class TestOtherSources:
    @pytest.mark.parametrize(
        "uri",
        [
            "",
            "/home/matt/aerial.tif",
            "C:/data/aerial.tif",
            "type=xyz&url=https://tile.example/{z}/{x}/{y}.png",
            "https://example.test/api/other-thing/abc/file.cog",
            "/vsicurl/https://example.test/api/tile-layer/",
        ],
    )
    def test_are_not_mistaken_for_a_portal_raster(self, uri: str) -> None:
        assert parse_tile_layer_uri(uri) is None

    def test_a_stacked_gdal_prefix_still_resolves(self) -> None:
        uri = f"/vsizip//vsicurl/{_PORTAL}/api/tile-layer/{_ITEM}/file.cog"
        assert parse_tile_layer_uri(uri) == (_PORTAL, _ITEM)

    def test_a_portal_on_a_subpath_keeps_its_whole_url(self) -> None:
        # Not every portal lives at a domain root.
        uri = f"/vsicurl/https://host.example/gis/api/tile-layer/{_ITEM}/file.cog"
        assert parse_tile_layer_uri(uri) == ("https://host.example/gis", _ITEM)
