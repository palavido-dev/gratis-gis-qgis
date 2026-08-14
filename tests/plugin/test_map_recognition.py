# SPDX-License-Identifier: AGPL-3.0-or-later
"""What publish-as-map recognises as already being on the portal.

The dialog reported an empty "included" list for a project full of
portal layers. It understood four providers, and none of them covered
a raster tile layer or an offline clone, so both were listed as local
files with an offer to publish them, which for a clone would have
pushed the portal's own data back at it as a new item.
"""
from __future__ import annotations

from gratisgis_qgis.browser.uris import (
    tile_layer_cog_uri,
    tile_layer_xyz_uri,
)
from gratisgis_qgis.publish.project_to_map import (
    CanvasLayer,
    CanvasViewport,
    PortalIndex,
    PortalTileLayerRef,
    ProjectSnapshot,
    translate,
)

_PORTAL = "https://gratisgis.org"
_RASTER = "bf03e789-a5c1-403c-b307-ea3252935cd8"


def _snapshot(*layers: CanvasLayer) -> ProjectSnapshot:
    return ProjectSnapshot(
        title="Map",
        layers=list(layers),
        viewport=CanvasViewport(center_lng=-79.8, center_lat=38.9, zoom=10.0),
    )


def _index() -> PortalIndex:
    return PortalIndex(
        tile_layers_by_item={
            _RASTER: PortalTileLayerRef(
                tile_url="cog://portal/x.cog", bbox_wgs84=(-80.0, 38.0, -79.0, 39.0)
            )
        }
    )


class TestPortalRasters:
    """The reported case: a portal raster on the canvas."""

    def test_a_cog_raster_is_included(self) -> None:
        snap = _snapshot(
            CanvasLayer(
                name="Imagery",
                source_uri=tile_layer_cog_uri(_PORTAL, _RASTER),
                provider="gdal",
                visible=True,
            )
        )
        result = translate(snap, portal_index=_index())
        assert result.skipped == []
        [layer] = result.data["layers"]
        assert layer["source"]["kind"] == "tile"
        assert layer["source"]["itemId"] == _RASTER
        assert layer["source"]["tileUrl"] == "cog://portal/x.cog"

    def test_an_unpacked_raster_is_included_too(self) -> None:
        # Same item, different storage, and the user cannot see which.
        snap = _snapshot(
            CanvasLayer(
                name="Imagery",
                source_uri=tile_layer_xyz_uri(_PORTAL, _RASTER),
                provider="wms",
                visible=True,
            )
        )
        result = translate(snap, portal_index=_index())
        assert result.skipped == []
        assert result.data["layers"][0]["source"]["itemId"] == _RASTER

    def test_the_extent_rides_along_when_known(self) -> None:
        snap = _snapshot(
            CanvasLayer(
                name="Imagery",
                source_uri=tile_layer_cog_uri(_PORTAL, _RASTER),
                provider="gdal",
                visible=True,
            )
        )
        result = translate(snap, portal_index=_index())
        assert result.data["layers"][0]["source"]["bboxWgs84"] == [
            -80.0,
            38.0,
            -79.0,
            39.0,
        ]

    def test_a_recognised_raster_with_no_details_says_so(self) -> None:
        # Better than emitting a layer the portal would draw as
        # nothing, and better than calling it a local file.
        snap = _snapshot(
            CanvasLayer(
                name="Imagery",
                source_uri=tile_layer_cog_uri(_PORTAL, _RASTER),
                provider="gdal",
                visible=True,
            )
        )
        result = translate(snap, portal_index=PortalIndex())
        assert result.data["layers"] == []
        assert "portal layer" in result.skipped[0].reason.lower()


class TestLayersKnownByOrigin:
    """An offline clone, or a layer this plugin just published."""

    def test_a_clone_contributes_its_source_layer(self) -> None:
        # NOT a republish of the local copy: the data came from the
        # portal, so the map points back at where it came from.
        snap = _snapshot(
            CanvasLayer(
                name="Trails (offline)",
                source_uri="C:/work/trails.gpkg|layername=trails",
                provider="ogr",
                visible=True,
                portal_item_id="item-1",
                portal_layer_key="trails",
            )
        )
        result = translate(snap)
        assert result.skipped == []
        assert result.data["layers"][0]["source"] == {
            "kind": "data-layer",
            "itemId": "item-1",
            "layerKey": "trails",
        }

    def test_a_single_layer_item_names_no_sublayer(self) -> None:
        snap = _snapshot(
            CanvasLayer(
                name="Trails (offline)",
                source_uri="C:/work/trails.gpkg|layername=trails",
                provider="ogr",
                visible=True,
                portal_item_id="item-1",
            )
        )
        result = translate(snap)
        assert result.data["layers"][0]["source"] == {
            "kind": "data-layer",
            "itemId": "item-1",
        }

    def test_a_plain_local_file_is_still_offered_for_publishing(self) -> None:
        snap = _snapshot(
            CanvasLayer(
                name="roads",
                source_uri="C:/work/roads.shp",
                provider="ogr",
                visible=True,
            )
        )
        result = translate(snap)
        assert result.data["layers"] == []
        assert result.skipped[0].is_local_vector
        assert "only on your computer" in result.skipped[0].reason


class TestSkipReasonsAvoidJargon:
    """These are read by someone publishing a map, not debugging QGIS."""

    def test_no_internal_vocabulary_leaks(self) -> None:
        snap = _snapshot(
            CanvasLayer(name="a", source_uri="x", provider="ogr", visible=True),
            CanvasLayer(name="b", source_uri="y", provider="wms", visible=True),
            CanvasLayer(name="c", source_uri="z", provider="mystery", visible=True),
            CanvasLayer(name="d", source_uri="w", provider="oapif", visible=True),
        )
        result = translate(snap)
        for skipped in result.skipped:
            reason = skipped.reason.lower()
            for jargon in (
                "phase",
                "data_layer",
                "oapif",
                "provider",
                "uri",
                "endpoint",
            ):
                assert jargon not in reason, f"{jargon!r} leaked into: {reason}"
