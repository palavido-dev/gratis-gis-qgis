# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the shared QGIS-provider URI builders."""
from __future__ import annotations

import pytest

from gratisgis_qgis.browser.uris import (
    authed_vector_tile_uri,
    oapif_uri,
    parse_vector_tile_uri,
    public_ogc_root,
    vector_tile_uri,
)


class TestPublicOgcRoot:
    def test_strips_trailing_slash(self) -> None:
        assert public_ogc_root("https://portal.example/") == \
            "https://portal.example/api/public/ogc"

    def test_strips_multiple_trailing_slashes(self) -> None:
        assert public_ogc_root("https://portal.example///") == \
            "https://portal.example/api/public/ogc"

    def test_keeps_path_components_in_portal_url(self) -> None:
        # Some portals run under a sub-path (rare but valid). The
        # builder shouldn't clobber an upstream path prefix.
        assert public_ogc_root("https://example/gis/") == \
            "https://example/gis/api/public/ogc"


class TestOapifUri:
    def test_emits_qgis_provider_shape(self) -> None:
        # QGIS's OAPIF provider parses `url='...' typename='...'`
        # The single quotes are part of the contract; QGIS uses
        # them to disambiguate spaces in URLs from key boundaries.
        # restrictToRequestBBOX + pageSize are appended so QGIS
        # uses viewport-bounded rendering on huge collections;
        # see uris.oapif_uri docstring.
        uri = oapif_uri("https://portal.example", "abc-123")
        assert "url='https://portal.example/api/public/ogc'" in uri
        assert "typename='abc-123'" in uri
        assert "restrictToRequestBBOX='1'" in uri
        assert "pageSize='1000'" in uri

    def test_passes_collection_id_through_unchanged(self) -> None:
        # Multi-layer collection ids use the `<itemId>__<layerKey>`
        # form per docs/ogc-api-strategy.md. The builder doesn't
        # alter the id (the caller supplies it).
        uri = oapif_uri("https://portal.example", "abc__roads")
        assert "typename='abc__roads'" in uri


class TestVectorTileUri:
    def test_emits_xyz_template_with_qgis_placeholders(self) -> None:
        # QGIS's vector-tile provider reads {z}/{y}/{x} from the
        # template. Our portal serves {tileMatrix}/{tileRow}/
        # {tileCol} which maps to z/y/x in the same order. The
        # zmin/zmax keys bound the zoom range so QGIS doesn't
        # probe every level on layer add.
        uri = vector_tile_uri("https://portal.example", "abc-123")
        assert (
            "type=xyz&url=https://portal.example/api/public/ogc"
            "/collections/abc-123/tiles/WebMercatorQuad/{z}/{y}/{x}"
        ) in uri
        assert "zmin=0" in uri
        assert "zmax=18" in uri

    def test_supports_layered_collection_ids(self) -> None:
        uri = vector_tile_uri("https://portal.example", "abc__roads")
        assert "collections/abc__roads/" in uri


class TestAuthedVectorTileUri:
    def test_emits_item_layer_route_with_authcfg(self) -> None:
        uri = authed_vector_tile_uri(
            "https://portal.example", "item-1", "roads", authcfg_id="abc1234"
        )
        assert uri.startswith(
            "type=xyz&url=https://portal.example"
            "/api/items/item-1/layers/roads/tile/"
        )
        assert "&authcfg=abc1234" in uri
        assert "zmin=0" in uri
        assert "zmax=18" in uri

    def test_strips_trailing_slash_from_portal_url(self) -> None:
        uri = authed_vector_tile_uri(
            "https://portal.example/", "i", "l", authcfg_id="a"
        )
        assert "example//" not in uri

    def test_tile_coordinate_orders_are_pinned_and_differ(self) -> None:
        # The two tile endpoints disagree on coordinate order: the
        # authed per-layer MVT route is z/x/y (the tile-server
        # convention the portal's map page uses) while the public
        # OGC Tiles surface is z/y/x (tileMatrix/tileRow/tileCol).
        # Swapping them renders scrambled tiles rather than an
        # obvious error, so BOTH orders are pinned explicitly.
        authed = authed_vector_tile_uri(
            "https://portal.example", "item-1", "roads", authcfg_id="a"
        )
        public = vector_tile_uri("https://portal.example", "item-1__roads")
        assert "/tile/{z}/{x}/{y}.mvt" in authed
        assert "/tiles/WebMercatorQuad/{z}/{y}/{x}" in public
        # Belt and braces: neither order appears in the other URI.
        assert "{z}/{y}/{x}" not in authed
        assert "{z}/{x}/{y}" not in public


class TestParseVectorTileUri:
    def test_round_trips_public_shape(self) -> None:
        uri = vector_tile_uri("https://portal.example", "abc__roads")
        assert parse_vector_tile_uri(uri) == ("https://portal.example", "abc__roads")

    def test_round_trips_authed_shape_as_joined_collection_id(self) -> None:
        # Publish-project's recognizer feeds the parsed collection id
        # through the same `<itemId>__<layerKey>` split as the public
        # shape, so a private layer added via the authed route still
        # maps back to its portal item.
        uri = authed_vector_tile_uri(
            "https://portal.example", "item-1", "roads", authcfg_id="abc1234"
        )
        assert parse_vector_tile_uri(uri) == ("https://portal.example", "item-1__roads")

    @pytest.mark.parametrize(
        "uri",
        [
            "",
            "not-a-uri",
            "type=xyz&url=https://elsewhere.example/tiles/{z}/{x}/{y}.png",
            # Authed-ish shape with the wrong path segments.
            "type=xyz&url=https://portal.example/api/items/i/sublayers/l/tile/{z}/{x}/{y}.mvt",
            # Missing ids.
            "type=xyz&url=https://portal.example/api/items//layers//tile/{z}/{x}/{y}.mvt",
        ],
    )
    def test_rejects_non_portal_shapes(self, uri: str) -> None:
        assert parse_vector_tile_uri(uri) is None


@pytest.mark.parametrize(
    "portal_url",
    [
        "https://portal.example",
        "https://portal.example/",
        "https://portal.example/gis",
    ],
)
def test_both_builders_use_the_same_root(portal_url: str) -> None:
    # Belt-and-suspenders: any change to public_ogc_root must
    # ripple cleanly through both Features and Tiles builders so
    # the OGC root stays one source of truth.
    root = public_ogc_root(portal_url)
    assert oapif_uri(portal_url, "abc").startswith(f"url='{root}' ")
    vt = vector_tile_uri(portal_url, "abc")
    assert "/tiles/WebMercatorQuad/{z}/{y}/{x}" in vt
    assert root in vt
