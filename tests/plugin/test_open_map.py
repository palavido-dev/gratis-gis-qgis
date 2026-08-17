# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opening a portal map: what gets built, what gets skipped, and why.

The planner is the risky half. It resolves every reference in the map
document into a QGIS layer URI using the same access rules the
Browser tree applies, and everything it cannot build has to surface as
a plain-English reason rather than a silently thinner map.
"""
from __future__ import annotations

from typing import Any

from gratisgis_qgis.open_map import (
    MapOpenPlan,
    PlannedLayer,
    SkippedMapLayer,
    plan_map_open,
    referenced_item_ids,
    scale_for_zoom,
)
from gratisgis_qgis.publish.project_to_map import zoom_for_scale

_PORTAL = "https://gratisgis.org"
_CFG = "lay1234"


def _map(layers: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "basemap": "",
        "center": [-80.06, 38.74],
        "zoom": 12,
        "layers": layers,
    }
    base.update(extra)
    return base


def _v3_item(item_id: str, access: str = "private") -> dict[str, Any]:
    return {
        "id": item_id,
        "access": access,
        "bbox": [-80.1, 38.7, -80.0, 38.8],
        "data": {
            "version": 3,
            "layers": [
                {"id": "parcels", "label": "Parcels", "geometryType": "Polygon"},
                {"id": "summary", "label": "Summary", "geometryType": None},
            ],
        },
    }


def _plan(
    layers: list[dict[str, Any]], referenced: dict[str, Any], **extra: Any
) -> Any:
    return plan_map_open(
        "Test map",
        _map(layers, **extra),
        referenced,
        portal_url=_PORTAL,
        layer_authcfg_id=_CFG,
    )


class TestDataLayerReferences:
    def test_a_private_sublayer_uses_the_authed_route(self) -> None:
        """Same rule as the Browser tree, deliberately.

        A map is mostly a bundle of the same layers the tree offers
        one by one; opening it must not invent a third access rule.
        """
        plan = _plan(
            [{"id": "a", "title": "Parcels",
              "source": {"kind": "data-layer", "itemId": "item-1",
                         "layerKey": "parcels"}}],
            {"item-1": _v3_item("item-1")},
        )
        assert isinstance(plan, MapOpenPlan)
        assert len(plan.layers) == 1
        assert isinstance(plan.layers[0], PlannedLayer)
        assert plan.layers[0].provider == "vectortile"
        assert f"authcfg={_CFG}" in plan.layers[0].uri
        assert "parcels" in plan.layers[0].uri

    def test_a_public_sublayer_stays_on_the_public_surface(self) -> None:
        """So a project saved from the opened map keeps rendering for
        someone who never signed in."""
        plan = _plan(
            [{"id": "a", "title": "Parcels",
              "source": {"kind": "data-layer", "itemId": "item-1",
                         "layerKey": "parcels"}}],
            {"item-1": _v3_item("item-1", access="public")},
        )
        assert "authcfg" not in plan.layers[0].uri

    def test_a_table_sublayer_is_skipped_with_directions(self) -> None:
        """A geometry-less table cannot be a canvas layer."""
        plan = _plan(
            [{"id": "a", "title": "Summary",
              "source": {"kind": "data-layer", "itemId": "item-1",
                         "layerKey": "summary"}}],
            {"item-1": _v3_item("item-1")},
        )
        assert not plan.layers
        assert "Clone" in plan.skipped[0].reason

    def test_an_unreachable_item_is_a_skip_not_a_crash(self) -> None:
        """Deleted item, or one the viewer cannot read: same outcome,
        an honest line instead of a mystery hole in the stack."""
        plan = _plan(
            [{"id": "a", "title": "Gone",
              "source": {"kind": "data-layer", "itemId": "item-x"}}],
            {"item-x": None},
        )
        assert not plan.layers
        assert isinstance(plan.skipped[0], SkippedMapLayer)
        assert "not reachable" in plan.skipped[0].reason


class TestOtherSources:
    def test_a_private_tile_layer_carries_the_credential(self) -> None:
        plan = _plan(
            [{"id": "a", "title": "Imagery",
              "source": {"kind": "tile", "itemId": "t-1", "tileUrl": "cog://x"}}],
            {"t-1": {"id": "t-1", "access": "org", "data": {}}},
        )
        assert plan.layers[0].provider == "wms"
        assert f"authcfg={_CFG}" in plan.layers[0].uri

    def test_a_public_tile_layer_does_not(self) -> None:
        plan = _plan(
            [{"id": "a", "title": "Imagery",
              "source": {"kind": "tile", "itemId": "t-1", "tileUrl": "cog://x"}}],
            {"t-1": {"id": "t-1", "access": "public", "data": {}}},
        )
        assert "authcfg" not in plan.layers[0].uri

    def test_arcgis_feature_and_map_services_pick_their_providers(self) -> None:
        plan = _plan(
            [
                {"id": "a", "title": "Roads",
                 "source": {"kind": "arcgis-rest", "url": "https://x/FS",
                            "layerId": 3, "serviceType": "FeatureServer"}},
                {"id": "b", "title": "Topo",
                 "source": {"kind": "arcgis-rest", "url": "https://x/MS",
                            "layerId": 0, "serviceType": "MapServer"}},
            ],
            {},
        )
        assert plan.layers[0].provider == "arcgisfeatureserver"
        assert "https://x/FS/3" in plan.layers[0].uri
        assert plan.layers[1].provider == "arcgismapserver"
        assert "layer='0'" in plan.layers[1].uri

    def test_a_credentialed_service_is_skipped_not_broken(self) -> None:
        """QGIS would call the upstream directly and be refused."""
        plan = _plan(
            [{"id": "a", "title": "Secure",
              "source": {"kind": "arcgis-rest", "url": "https://x/FS",
                         "layerId": 1, "serviceType": "FeatureServer",
                         "proxyUrl": "/api/portal/items/s/proxy"}}],
            {},
        )
        assert not plan.layers
        assert "portal" in plan.skipped[0].reason

    def test_web_geojson_is_refused_because_of_the_deadlock(self) -> None:
        """/vsicurl in a saved project is the #24 freeze. Never again
        by the front door, so never by the side door either."""
        plan = _plan(
            [{"id": "a", "title": "Feed",
              "source": {"kind": "geojson-url", "url": "https://x/f.json"}}],
            {},
        )
        assert not plan.layers
        assert plan.skipped

    def test_every_unsupported_kind_names_itself(self) -> None:
        plan = _plan(
            [
                {"id": "a", "title": "Inline",
                 "source": {"kind": "geojson-inline", "geojson": {}}},
                {"id": "b", "title": "Live DB",
                 "source": {"kind": "postgis-live"}},
                {"id": "c", "title": "Cloud",
                 "source": {"kind": "point-cloud"}},
            ],
            {},
        )
        assert len(plan.skipped) == 3
        assert all(s.reason for s in plan.skipped)


class TestStackShape:
    def test_order_visibility_opacity_and_groups_survive(self) -> None:
        """Layers[0] is the top of the portal stack and must stay on
        top in QGIS; a flipped stack looks subtly wrong everywhere."""
        plan = _plan(
            [
                {"id": "g1", "title": "Overlays", "source": {"kind": "group"}},
                {"id": "a", "title": "Top", "visible": False, "opacity": 0.5,
                 "groupId": "g1",
                 "source": {"kind": "data-layer", "itemId": "item-1",
                            "layerKey": "parcels"}},
                {"id": "b", "title": "Bottom",
                 "source": {"kind": "tile", "itemId": "t-1"}},
            ],
            {"item-1": _v3_item("item-1"),
             "t-1": {"id": "t-1", "access": "public", "data": {}}},
        )
        assert [p.title for p in plan.layers] == ["Top", "Bottom"]
        assert plan.layers[0].visible is False
        assert plan.layers[0].opacity == 0.5
        assert plan.layers[0].group == "Overlays"
        assert plan.layers[1].group == ""

    def test_the_basemap_resolves_to_a_bottom_xyz_layer(self) -> None:
        plan = _plan(
            [],
            {"bm-1": {"id": "bm-1", "title": "Streets", "access": "public",
                      "data": {"tileUrl": "https://tiles.example/{z}/{x}/{y}.png"}}},
            basemap="bm-1",
        )
        assert plan.basemap is not None
        assert plan.basemap.provider == "wms"
        assert "tiles.example" in plan.basemap.uri

    def test_a_style_url_basemap_is_left_out_quietly(self) -> None:
        """A MapLibre style document has no QGIS representation; a
        broken bottom layer would be worse than none."""
        plan = _plan(
            [],
            {"bm-1": {"id": "bm-1", "title": "Vector style",
                      "data": {"styleUrl": "https://x/style.json"}}},
            basemap="bm-1",
        )
        assert plan.basemap is None

    def test_the_camera_round_trips_through_publish_math(self) -> None:
        """Publish turns scale into zoom; open turns zoom back. A map
        published from QGIS and reopened must land where it left."""
        lat = 38.74
        scale = 24000.0
        zoom = zoom_for_scale(scale, lat)
        assert abs(scale_for_zoom(zoom, lat) - scale) < 0.01
        plan = _plan([], {})
        assert plan.view is not None
        lon, plat, pscale = plan.view
        assert (lon, plat) == (-80.06, 38.74)
        assert pscale > 0

    def test_a_map_with_no_camera_has_no_view(self) -> None:
        plan = plan_map_open(
            "m", {"layers": []}, {}, portal_url=_PORTAL, layer_authcfg_id=_CFG
        )
        assert plan.view is None


class TestReferencedItemIds:
    def test_only_fetchable_kinds_cost_a_fetch(self) -> None:
        ids = referenced_item_ids(_map(
            [
                {"id": "a", "source": {"kind": "data-layer", "itemId": "d-1"}},
                {"id": "b", "source": {"kind": "tile", "itemId": "t-1"}},
                {"id": "c", "source": {"kind": "geojson-url", "url": "x"}},
                {"id": "d", "source": {"kind": "data-layer", "itemId": "d-1"}},
            ],
            basemap="bm-1",
        ))
        assert ids == ["bm-1", "d-1", "t-1"]


import pytest  # noqa: E402  (fixture support for the dialog tests)

from tests.plugin.conftest import install_qgis_stub  # noqa: E402


@pytest.fixture
def dlg_mod(monkeypatch: Any) -> Any:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.PyQt.QtCore": {
                "Qt": type(
                    "Qt", (),
                    {"ItemDataRole": type("R", (), {"UserRole": 32})},
                ),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": {
                name: type(name, (), {})
                for name in (
                    "QComboBox", "QDialog", "QDialogButtonBox", "QLabel",
                    "QListWidget", "QListWidgetItem", "QVBoxLayout",
                    "QWidget",
                )
            },
        },
    )
    from gratisgis_qgis.ui import open_map_dialog

    return open_map_dialog


class TestOpenMapDialogPieces:
    """The picker's decisions, without its widgets."""

    def test_map_rows_keeps_only_maps_in_portal_order(
        self, dlg_mod: Any
    ) -> None:
        from types import SimpleNamespace

        open_map_dialog = dlg_mod

        items = [
            SimpleNamespace(id="a", type="map", title="First", access="org"),
            SimpleNamespace(id="b", type="data_layer", title="Not me",
                            access="org"),
            SimpleNamespace(id="c", type="map", title="", access=""),
        ]
        assert open_map_dialog.map_rows(items) == [
            ("a", "First", "org"),
            ("c", "Untitled map", ""),
        ]

    def test_open_launches_the_flow_for_the_picked_row(
        self, dlg_mod: Any, monkeypatch: Any
    ) -> None:
        from types import SimpleNamespace

        import gratisgis_qgis.open_map as open_map_mod

        open_map_dialog = dlg_mod

        launched: list[tuple[str, str]] = []
        monkeypatch.setattr(
            open_map_mod,
            "launch_open_map",
            lambda _p, item_id, title, _i: launched.append((item_id, title)),
        )
        dlg = open_map_dialog.OpenMapDialog.__new__(
            open_map_dialog.OpenMapDialog
        )
        dlg._iface = SimpleNamespace()
        dlg._signed_in = ["demo"]
        dlg._store = SimpleNamespace(  # type: ignore[assignment]
            get=lambda _n: SimpleNamespace(name="demo")
        )
        dlg._connection_combo = SimpleNamespace(currentIndex=lambda: 0)
        dlg._list = SimpleNamespace(
            currentItem=lambda: SimpleNamespace(
                data=lambda _r: ("m-1", "WV overview")
            )
        )
        dlg.accept = lambda: None  # type: ignore[method-assign]
        dlg._open()
        assert launched == [("m-1", "WV overview")]

    def test_open_with_no_selection_asks_for_one(self, dlg_mod: Any) -> None:
        from types import SimpleNamespace

        open_map_dialog = dlg_mod

        dlg = open_map_dialog.OpenMapDialog.__new__(
            open_map_dialog.OpenMapDialog
        )
        told: list[str] = []
        dlg._list = SimpleNamespace(currentItem=lambda: None)
        dlg._status = SimpleNamespace(setText=told.append)
        dlg._open()
        assert told and "Pick a map" in told[0]


class TestCanvasCamera:
    """The saved viewport must survive QGIS's own first-layer zoom.

    The canvas bridge auto-zooms to the first layer's full extent
    through a deferred call, and a vector tile layer's full extent is
    the whole world. A camera set synchronously during layer adds was
    therefore thrown away a moment later: every portal map opened at
    world extent. The fix queues the camera set behind the bridge's.
    """

    def test_the_camera_is_deferred_and_lands_on_the_saved_view(
        self, monkeypatch: Any
    ) -> None:
        from types import SimpleNamespace

        from tests.plugin.conftest import install_qgis_stub

        delays: list[int] = []

        class _QTimer:
            @staticmethod
            def singleShot(ms: int, fn: Any) -> None:
                delays.append(ms)
                fn()

        class _Transform:
            def __init__(self, *_args: Any) -> None:
                pass

            def transform(self, point: Any) -> Any:
                return point

        install_qgis_stub(
            monkeypatch,
            {
                "qgis.PyQt.QtCore": {"QTimer": _QTimer},
                "qgis.core": {
                    "QgsCoordinateReferenceSystem": lambda *_a: object(),
                    "QgsCoordinateTransform": _Transform,
                    "QgsPointXY": lambda lon, lat: (lon, lat),
                    "QgsProject": SimpleNamespace(
                        instance=lambda: SimpleNamespace()
                    ),
                },
            },
        )
        calls: list[tuple[Any, ...]] = []
        canvas = SimpleNamespace(
            mapSettings=lambda: SimpleNamespace(
                destinationCrs=lambda: "canvas-crs"
            ),
            setCenter=lambda p: calls.append(("center", p)),
            zoomScale=lambda s: calls.append(("scale", s)),
            refresh=lambda: calls.append(("refresh",)),
        )
        iface = SimpleNamespace(mapCanvas=lambda: canvas)

        from gratisgis_qgis.open_map import _point_canvas_when_settled

        _point_canvas_when_settled(iface, (-80.06, 38.74, 9000.0))

        # Queued (delay 0), not applied synchronously, and once run it
        # centers, scales, and repaints in that order.
        assert delays == [0]
        assert calls == [
            ("center", (-80.06, 38.74)),
            ("scale", 9000.0),
            ("refresh",),
        ]

    def test_a_broken_canvas_is_logged_not_raised(
        self, monkeypatch: Any
    ) -> None:
        from types import SimpleNamespace

        from tests.plugin.conftest import install_qgis_stub

        class _QTimer:
            @staticmethod
            def singleShot(_ms: int, fn: Any) -> None:
                fn()

        install_qgis_stub(
            monkeypatch, {"qgis.PyQt.QtCore": {"QTimer": _QTimer}}
        )

        def explode() -> Any:
            raise RuntimeError("no canvas")

        iface = SimpleNamespace(mapCanvas=explode)

        from gratisgis_qgis.open_map import _point_canvas_when_settled

        # Must not raise: a camera miss is a logged nuisance, not a
        # failed map open.
        _point_canvas_when_settled(iface, (0.0, 0.0, 1000.0))
