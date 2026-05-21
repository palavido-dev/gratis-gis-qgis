# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the publish-project-as-map translator.

The translator is pure-Python by design (the publish dialog
gathers QGIS state and hands in dataclasses) so these tests
exercise it without a QGIS runtime in the import path.
"""
from __future__ import annotations

import pytest

from gratisgis_qgis.browser.uris import oapif_uri, vector_tile_uri
from gratisgis_qgis.publish.project_to_map import (
    CanvasLayer,
    CanvasViewport,
    MapTranslation,
    ProjectSnapshot,
    SkippedLayer,
    translate,
)


PORTAL = "https://portal.example"


def _snapshot(*layers: CanvasLayer, title: str = "Test map") -> ProjectSnapshot:
    return ProjectSnapshot(
        title=title,
        layers=list(layers),
        viewport=CanvasViewport(center_lng=-80.5, center_lat=38.2, zoom=10.0),
    )


def _oapif_layer(item_id: str, name: str = "Parcels", **kw) -> CanvasLayer:
    return CanvasLayer(
        name=name,
        source_uri=oapif_uri(PORTAL, item_id),
        provider="OAPIF",
        visible=kw.get("visible", True),
        opacity=kw.get("opacity", 1.0),
    )


def _tile_layer(item_id: str, name: str = "Parcels MVT", **kw) -> CanvasLayer:
    return CanvasLayer(
        name=name,
        source_uri=vector_tile_uri(PORTAL, item_id),
        provider="vectortile",
        visible=kw.get("visible", True),
        opacity=kw.get("opacity", 1.0),
    )


class TestRecognizedLayers:
    def test_oapif_layer_resolves_to_data_layer_source(self) -> None:
        # OAPIF is the canonical provider for portal data_layer items,
        # so the translator should produce a {"kind": "data-layer",
        # "itemId": ...} source block.
        snap = _snapshot(_oapif_layer("abc-123", "Parcels"))
        result = translate(snap)

        assert len(result.data["layers"]) == 1
        layer = result.data["layers"][0]
        assert layer["title"] == "Parcels"
        assert layer["visible"] is True
        assert layer["opacity"] == 1.0
        assert layer["source"] == {"kind": "data-layer", "itemId": "abc-123"}
        assert result.skipped == []

    def test_vector_tile_layer_resolves_to_data_layer_source(self) -> None:
        # Tile-served layers project onto data-layer too (see comment
        # in _resolve_layer). This test pins that behavior so the day
        # we add a real "tile-layer" source kind we'll trip here and
        # update consciously rather than silently re-shape payloads.
        snap = _snapshot(_tile_layer("xyz-789", "Roads"))
        result = translate(snap)

        assert len(result.data["layers"]) == 1
        layer = result.data["layers"][0]
        assert layer["source"] == {"kind": "data-layer", "itemId": "xyz-789"}
        assert result.skipped == []

    def test_layer_id_is_namespaced_by_qgis_name(self) -> None:
        # The portal MapData layers carry a stable per-layer id; we
        # synthesize one from the QGIS layer name so re-publishing the
        # same project yields a diffable shape.
        snap = _snapshot(_oapif_layer("abc-123", "My Parcels"))
        result = translate(snap)
        assert result.data["layers"][0]["id"] == "qgis-My Parcels"

    def test_layered_collection_id_passes_through(self) -> None:
        # Multi-layer items use the `<itemId>__<layerKey>` collection
        # id form per docs/ogc-api-strategy.md. The translator just
        # echoes whatever id was encoded in the URI.
        snap = _snapshot(_oapif_layer("abc__roads", "Roads"))
        result = translate(snap)
        assert result.data["layers"][0]["source"]["itemId"] == "abc__roads"

    def test_visibility_and_opacity_propagate(self) -> None:
        snap = _snapshot(
            _oapif_layer("a", "Hidden", visible=False, opacity=0.5),
        )
        out = translate(snap).data["layers"][0]
        assert out["visible"] is False
        assert out["opacity"] == 0.5

    def test_opacity_is_clamped_to_unit_interval(self) -> None:
        # Belt-and-suspenders: opacity values from old projects can
        # exceed [0, 1] (especially the QML 0..255 convention if a
        # caller forgets to normalize). The translator should never
        # let an out-of-range value reach the portal.
        snap = _snapshot(
            CanvasLayer(
                name="OverBright",
                source_uri=oapif_uri(PORTAL, "a"),
                provider="OAPIF",
                visible=True,
                opacity=4.2,
            ),
            CanvasLayer(
                name="Negative",
                source_uri=oapif_uri(PORTAL, "b"),
                provider="OAPIF",
                visible=True,
                opacity=-0.3,
            ),
        )
        result = translate(snap)
        opacities = [lyr["opacity"] for lyr in result.data["layers"]]
        assert opacities == [1.0, 0.0]


class TestSkippedLayers:
    def test_unknown_provider_is_skipped(self) -> None:
        snap = _snapshot(
            CanvasLayer(
                name="Mystery",
                source_uri="something://opaque",
                provider="mystery",
                visible=True,
            )
        )
        result = translate(snap)
        assert result.data["layers"] == []
        assert len(result.skipped) == 1
        skipped = result.skipped[0]
        assert isinstance(skipped, SkippedLayer)
        assert skipped.name == "Mystery"
        assert "mystery" in skipped.reason.lower() or "unsupported" in skipped.reason.lower()

    def test_external_service_layer_has_specific_skip_reason(self) -> None:
        # External services (WMS, ArcGIS Feature Server, etc.) are a
        # known-but-unsupported case. The reason text should hint at
        # the workaround (register the service as a portal item) so
        # the dialog doesn't just say "unsupported" and leave the
        # user to guess.
        snap = _snapshot(
            CanvasLayer(
                name="Census Tracts",
                source_uri="contextualWMSLegend=0&...",
                provider="wms",
                visible=True,
            )
        )
        result = translate(snap)
        assert len(result.skipped) == 1
        reason = result.skipped[0].reason.lower()
        assert "external" in reason
        assert "portal" in reason

    def test_local_file_layer_has_specific_skip_reason(self) -> None:
        # OGR (shapefile, GeoJSON, etc.) and memory layers can't be
        # published as-is; the user needs to ingest the data through
        # the Phase 3 vector-publish flow first. The skip reason
        # should point at that.
        snap = _snapshot(
            CanvasLayer(
                name="My Shapefile",
                source_uri="/Users/me/data/parcels.shp",
                provider="ogr",
                visible=True,
            )
        )
        result = translate(snap)
        assert len(result.skipped) == 1
        reason = result.skipped[0].reason.lower()
        assert "publish" in reason or "data_layer" in reason

    def test_oapif_layer_pointing_at_unknown_host_is_skipped(self) -> None:
        # An OAPIF URI is necessary but not sufficient: if the URL
        # doesn't end in our `/api/public/ogc` suffix the translator
        # has no way to know which portal item it maps to.
        snap = _snapshot(
            CanvasLayer(
                name="Foreign OAPIF",
                source_uri="url='https://elsewhere.example/oapif' typename='foo'",
                provider="OAPIF",
                visible=True,
            )
        )
        result = translate(snap)
        assert result.data["layers"] == []
        assert len(result.skipped) == 1

    def test_vector_tile_with_wrong_template_is_skipped(self) -> None:
        snap = _snapshot(
            CanvasLayer(
                name="Foreign MVT",
                source_uri="type=xyz&url=https://elsewhere/tiles/{z}/{x}/{y}",
                provider="vectortile",
                visible=True,
            )
        )
        result = translate(snap)
        assert result.data["layers"] == []
        assert len(result.skipped) == 1


class TestLayerOrder:
    def test_layer_order_is_preserved(self) -> None:
        # Portal MapData.layers and QGIS layer-tree share the same
        # bottom-up draw convention, and the publish dialog hands the
        # snapshot in canvas order. The translator must not reorder.
        snap = _snapshot(
            _oapif_layer("a", "Bottom"),
            _oapif_layer("b", "Middle"),
            _oapif_layer("c", "Top"),
        )
        names = [lyr["title"] for lyr in translate(snap).data["layers"]]
        assert names == ["Bottom", "Middle", "Top"]

    def test_skipped_layers_do_not_shift_published_order(self) -> None:
        # Skipped layers are removed from the output entirely; the
        # remaining layers preserve their relative order.
        snap = _snapshot(
            _oapif_layer("a", "First"),
            CanvasLayer(
                name="Skipped",
                source_uri="/tmp/foo.shp",
                provider="ogr",
                visible=True,
            ),
            _oapif_layer("b", "Last"),
        )
        result = translate(snap)
        names = [lyr["title"] for lyr in result.data["layers"]]
        assert names == ["First", "Last"]
        assert [s.name for s in result.skipped] == ["Skipped"]


class TestViewportAndEnvelope:
    def test_viewport_is_emitted_in_lng_lat_order(self) -> None:
        # MapData.view.center is [lng, lat]; the dialog hands us the
        # already-reprojected coordinates so the translator just
        # passes them through. Pinning order here so a future
        # refactor doesn't silently flip them.
        snap = _snapshot()
        result = translate(snap)
        assert result.data["view"]["center"] == [-80.5, 38.2]
        assert result.data["view"]["zoom"] == 10.0

    def test_envelope_keys_match_portal_map_data_shape(self) -> None:
        # The MapData envelope on the portal carries {version,
        # basemap, layers, view}. We always emit version=1 and an
        # empty basemap (the portal's default basemap kicks in when
        # the string is empty), so re-publishing doesn't fight the
        # user's portal-level basemap default.
        result = translate(_snapshot())
        assert result.data["version"] == 1
        assert result.data["basemap"] == ""
        assert "layers" in result.data
        assert "view" in result.data

    def test_empty_project_still_produces_valid_envelope(self) -> None:
        # An empty map is a legitimate starting point: the user can
        # publish first, add layers from the portal UI later. The
        # translator should not raise.
        result = translate(_snapshot())
        assert isinstance(result, MapTranslation)
        assert result.data["layers"] == []
        assert result.skipped == []


class TestProviderCaseInsensitivity:
    @pytest.mark.parametrize("provider", ["OAPIF", "oapif", "OApif"])
    def test_oapif_provider_match_is_case_insensitive(self, provider: str) -> None:
        # QGIS provider strings vary by version: older builds emit
        # 'OAPIF', some scripts produce 'oapif'. The recognizer
        # normalizes to upper() so we don't drop layers over casing.
        snap = _snapshot(
            CanvasLayer(
                name="Parcels",
                source_uri=oapif_uri(PORTAL, "a"),
                provider=provider,
                visible=True,
            )
        )
        assert len(translate(snap).data["layers"]) == 1

    @pytest.mark.parametrize("provider", ["vectortile", "VectorTile", "VECTORTILE"])
    def test_vectortile_provider_match_is_case_insensitive(self, provider: str) -> None:
        snap = _snapshot(
            CanvasLayer(
                name="Tiles",
                source_uri=vector_tile_uri(PORTAL, "a"),
                provider=provider,
                visible=True,
            )
        )
        assert len(translate(snap).data["layers"]) == 1
