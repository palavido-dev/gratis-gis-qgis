# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the shared QGIS-provider URI builders."""
from __future__ import annotations

import pytest

from gratisgis_qgis.browser.uris import (
    oapif_uri,
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
