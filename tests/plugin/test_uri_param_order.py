# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recognising a portal layer whose URI QGIS has reordered.

Reported: publish-as-map listed a hillshade sitting in the portal's own
Browser tree as "an outside service the portal does not know about".

A provider URI is an unordered parameter bag, and QGIS rewrites it to
suit itself. Both parsers keyed off the string STARTING with
``type=xyz``, which is how our builders spell it and not how QGIS spells
it back. A reordered URI stopped being recognised as a portal layer at
all. The layer still drew, so nothing looked broken; it just became
invisible to everything that asks "is this ours", which is
publish-as-map, the clone picker, the sync picker and the load trace.

Every source string below with ``authcfg=`` first is copied verbatim
from the plugin's own log of Matt's session. That is deliberate: the
suite had plenty of URI tests and all of them used the order our
builders emit, which is exactly the order that already worked.
"""
from __future__ import annotations

import pytest

from gratisgis_qgis.browser.uris import (
    authed_vector_tile_uri,
    parse_portal_layer_source,
    parse_tile_layer_uri,
    parse_vector_tile_uri,
    tile_layer_cog_uri,
    tile_layer_xyz_uri,
    uri_param,
)

_PORTAL = "https://gratisgis.org"
_RASTER_ITEM = "ed98bb41-053d-4317-897d-bf124d6a9dcd"
_VECTOR_ITEM = "71ec2071-1243-4621-a0cb-623edfebd467"
_LAYER = "lyr_1ch2vc9x"

#: Verbatim from plugin.log: the hillshade that was not recognised.
REORDERED_RASTER = (
    "authcfg=e53df68&type=xyz&url=https%3A%2F%2Fgratisgis.org%2Fapi%2F"
    "tile-layer%2Fed98bb41-053d-4317-897d-bf124d6a9dcd%2Ftiles%2F"
    "%7Bz%7D%2F%7Bx%7D%2F%7By%7D.png&zmax=18&zmin=0"
)


class TestUriParam:
    def test_a_parameter_is_found_wherever_it_sits(self) -> None:
        assert uri_param("a=1&b=2&c=3", "b") == "2"
        assert uri_param("b=2&a=1", "b") == "2"

    def test_the_value_is_unquoted(self) -> None:
        """The URL inside is percent-encoded, which is what makes the
        split on ``&`` safe in the first place."""
        assert uri_param("url=https%3A%2F%2Fx.example%2Fa", "url") == (
            "https://x.example/a"
        )

    def test_a_missing_parameter_is_none(self) -> None:
        assert uri_param("type=xyz&url=x", "authcfg") is None

    def test_a_name_that_only_appears_inside_a_value_is_not_matched(
        self,
    ) -> None:
        """A URL containing "url=" as a query string must not be picked
        up as the parameter itself."""
        assert uri_param("type=xyz&other=http://x/?url=trap", "url") is None

    def test_a_bare_path_has_no_parameters(self) -> None:
        assert uri_param("/vsicurl/https://x.example/file.cog", "url") is None


class TestRasterTileLayer:
    def test_the_reordered_hillshade_is_recognised(self) -> None:
        """The exact string from the log, and the whole bug."""
        assert parse_tile_layer_uri(REORDERED_RASTER) == (
            _PORTAL, _RASTER_ITEM
        )

    def test_the_order_our_builder_emits_still_works(self) -> None:
        built = tile_layer_xyz_uri(
            _PORTAL, _RASTER_ITEM, authcfg_id="e53df68"
        )
        assert parse_tile_layer_uri(built) == (_PORTAL, _RASTER_ITEM)

    def test_the_cog_shape_still_works(self) -> None:
        """No url parameter at all; it is a plain GDAL path.

        The fix must not break the shape that was already fine, which
        is the one that WAS recognised in the same dialog.
        """
        assert parse_tile_layer_uri(
            tile_layer_cog_uri(_PORTAL, _RASTER_ITEM)
        ) == (_PORTAL, _RASTER_ITEM)

    @pytest.mark.parametrize(
        "uri",
        [
            "zmin=0&zmax=18&type=xyz&url=https%3A%2F%2Fgratisgis.org%2Fapi%2Ftile-layer%2Fed98bb41-053d-4317-897d-bf124d6a9dcd%2Ftiles%2F%7Bz%7D.png",
            "url=https%3A%2F%2Fgratisgis.org%2Fapi%2Ftile-layer%2Fed98bb41-053d-4317-897d-bf124d6a9dcd%2Ftiles%2F%7Bz%7D.png&type=xyz",
            "type=xyz&crs=EPSG%3A3857&url=https%3A%2F%2Fgratisgis.org%2Fapi%2Ftile-layer%2Fed98bb41-053d-4317-897d-bf124d6a9dcd%2Ftiles%2F%7Bz%7D.png",
        ],
        ids=["zoom-first", "url-first", "crs-inserted"],
    )
    def test_any_parameter_order_is_recognised(self, uri: str) -> None:
        """QGIS is free to write these in any order it likes.

        Pinning one order is what produced the bug, so the test pins
        the absence of an order instead.
        """
        assert parse_tile_layer_uri(uri) == (_PORTAL, _RASTER_ITEM)

    def test_an_unrelated_xyz_layer_is_still_not_ours(self) -> None:
        """The USGS basemap on the same canvas must stay unrecognised.

        A parser made too eager here would claim other people's tile
        services as portal items and build a map referencing them as
        though the portal knew about them.
        """
        usgs = (
            "type=xyz&url=https%3A%2F%2Fbasemap.nationalmap.gov%2Farcgis"
            "%2Frest%2Fservices%2FUSGSImageryOnly%2FMapServer%2Ftile%2F"
            "%7Bz%7D%2F%7By%7D%2F%7Bx%7D"
        )
        assert parse_tile_layer_uri(usgs) is None


class TestVectorTileLayer:
    """The same gate, the same latent bug, not yet triggered.

    Matt's MVT layers were recognised because their sources still
    happened to start with ``type=xyz&url=``. A project reload could
    reorder them exactly as it reordered the raster, and the clone and
    sync pickers would then stop offering them.
    """

    def test_a_reordered_authed_mvt_is_recognised(self) -> None:
        reordered = (
            f"authcfg=e53df68&type=xyz&zmin=0&url={_PORTAL}"
            f"/api/items/{_VECTOR_ITEM}/layers/{_LAYER}/tile/"
            "{z}/{x}/{y}.mvt"
        )
        assert parse_vector_tile_uri(reordered) == (
            _PORTAL, f"{_VECTOR_ITEM}__{_LAYER}"
        )

    def test_the_order_our_builder_emits_still_works(self) -> None:
        built = authed_vector_tile_uri(
            _PORTAL, _VECTOR_ITEM, _LAYER, authcfg_id="e53df68"
        )
        assert parse_vector_tile_uri(built) == (
            _PORTAL, f"{_VECTOR_ITEM}__{_LAYER}"
        )

    def test_a_reordered_public_tile_layer_is_recognised(self) -> None:
        reordered = (
            f"zmax=18&type=xyz&url={_PORTAL}"
            f"/api/public/ogc/collections/{_VECTOR_ITEM}"
            "/tiles/WebMercatorQuad/{z}/{y}/{x}"
        )
        assert parse_vector_tile_uri(reordered) == (_PORTAL, _VECTOR_ITEM)

    def test_the_clone_and_sync_pickers_see_it_too(self) -> None:
        """They resolve through the same parser.

        A layer the pickers do not recognise cannot be cloned or
        synced, and the dialog reports "no portal-backed layers in
        project" rather than naming what it rejected.
        """
        reordered = (
            f"authcfg=e53df68&type=xyz&url={_PORTAL}"
            f"/api/items/{_VECTOR_ITEM}/layers/{_LAYER}/tile/"
            "{z}/{x}/{y}.mvt"
        )
        ref = parse_portal_layer_source(reordered)
        assert ref is not None
        assert ref.item_id == _VECTOR_ITEM
        assert ref.layer_id == _LAYER

    def test_an_off_portal_vector_tile_service_is_not_claimed(self) -> None:
        other = (
            "authcfg=x&type=xyz&url=https%3A%2F%2Ftiles.example.com"
            "%2Fv1%2F%7Bz%7D%2F%7Bx%7D%2F%7By%7D.mvt"
        )
        assert parse_vector_tile_uri(other) is None
