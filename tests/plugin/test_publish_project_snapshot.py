# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading the QGIS project into the shape publish-as-map translates.

``publish/project_to_map.py`` is well covered, but it is only as good
as what it is handed, and the code that builds that input lived in a
503-statement dialog module no test had ever imported. The translation
being right is no comfort if the snapshot feeding it names the wrong
item, loses a layer's opacity, or misses that a layer is already on the
portal.

The recurring failure in this area has one shape: a layer that IS
portal data but does not look like it from its URI. An offline clone is
a local GeoPackage. A layer this plugin published is whatever it always
was. Both were offered for publishing a second time, and for a clone
that meant pushing the portal's own data back at it as a new item.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub

# Exactly the widget names publish_project_dialog imports at module
# level. Listed rather than guessed generously, so a name that stops
# being imported shows up as an unused stub instead of hiding among
# spares.
_WIDGETS = [
    "QComboBox", "QDialog", "QDialogButtonBox", "QFormLayout",
    "QHBoxLayout", "QLabel", "QLineEdit", "QListWidget",
    "QListWidgetItem", "QMessageBox", "QPlainTextEdit", "QPushButton",
    "QVBoxLayout", "QWidget",
]


class _StubQt:
    """The scoped-enum spellings the dialog uses.

    Scoped, not the QGIS 3 class-level shortcuts: PyQt6 dropped those
    and the production code is written for the spelling that works on
    both. A stub offering the old shortcuts would let a regression to
    them pass here and fail on QGIS 4.
    """

    class ItemDataRole:
        UserRole = 32

    class CheckState:
        Unchecked = 0
        Checked = 2


class _StubProject:
    """A QgsProject stand-in with a settable layer set and title."""

    instance_obj: _StubProject | None = None

    def __init__(self) -> None:
        self._layers: dict[str, Any] = {}
        self._title = ""
        self._tree = SimpleNamespace(findLayers=lambda: [])

    @classmethod
    def instance(cls) -> _StubProject:
        assert cls.instance_obj is not None
        return cls.instance_obj

    def mapLayers(self) -> dict[str, Any]:
        return self._layers

    def layerTreeRoot(self) -> Any:
        return self._tree

    def title(self) -> str:
        return self._title


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _StubProject.instance_obj = _StubProject()
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsProject": _StubProject,
                "QgsCoordinateReferenceSystem": _StubCrs,
                "QgsCoordinateTransform": _StubTransform,
            },
            "qgis.PyQt.QtCore": {
                "Qt": _StubQt,
                "QSize": type("QSize", (), {}),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": {n: type(n, (), {}) for n in _WIDGETS},
        },
    )
    import gratisgis_qgis.ui.publish_project_dialog as m

    return m


class _StubCrs:
    def __init__(self, authid: str = "EPSG:4326") -> None:
        self._authid = authid

    def isValid(self) -> bool:
        return bool(self._authid)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _StubCrs) and other._authid == self._authid


class _StubTransform:
    """Records that a reprojection happened, and shifts the point.

    The shift is arbitrary and only has to be visible: the assertion is
    that the transform was applied at all, not what it computes.
    """

    def __init__(self, src: Any, dst: Any, project: Any) -> None:
        self.src = src

    def transform(self, point: Any) -> Any:
        return SimpleNamespace(x=lambda: 111.0, y=lambda: 22.0)


class _Layer:
    """The parts of a QGIS map layer the snapshot reads."""

    def __init__(
        self,
        name: str = "Parcels",
        source: str = "/home/matt/parcels.shp",
        provider: str = "ogr",
        layer_id: str = "L1",
        properties: dict[str, Any] | None = None,
        opacity: float | None = None,
        renderer_opacity: float | None = None,
    ) -> None:
        self._name = name
        self._source = source
        self._provider = provider
        self._id = layer_id
        self._props = properties or {}
        if opacity is not None:
            self.opacity = lambda: opacity  # type: ignore[assignment]
        if renderer_opacity is not None:
            self.renderer = lambda: SimpleNamespace(  # type: ignore[assignment]
                opacity=lambda: renderer_opacity
            )

    def name(self) -> str:
        return self._name

    def source(self) -> str:
        return self._source

    def providerType(self) -> str:
        return self._provider

    def id(self) -> str:
        return self._id

    def customProperty(self, key: str, default: Any = None) -> Any:
        return self._props.get(key, default)


class TestFormatSource:
    """The line the user reads to check what a layer will point at."""

    def test_a_data_layer_names_its_item_and_sublayer(
        self, mod: ModuleType
    ) -> None:
        line = mod._format_source(
            {"kind": "data-layer", "itemId": "item-1", "layerKey": "roads"}
        )
        assert "item-1" in line and "roads" in line

    def test_a_single_layer_item_names_no_sublayer(
        self, mod: ModuleType
    ) -> None:
        line = mod._format_source({"kind": "data-layer", "itemId": "item-1"})
        assert "item-1" in line
        assert "/" not in line

    def test_an_untracked_arcgis_service_is_called_out(
        self, mod: ModuleType
    ) -> None:
        """A working reference the portal admin cannot see.

        It will render, so nothing looks wrong, and it is invisible to
        anyone auditing what the org depends on.
        """
        line = mod._format_source(
            {
                "kind": "arcgis-rest",
                "url": "https://svc.example/MapServer",
                "layerId": 3,
                "serviceType": "MapServer",
            }
        )
        assert "not a portal item" in line
        assert "svc.example" in line

    def test_a_tracked_arcgis_service_names_the_portal_item(
        self, mod: ModuleType
    ) -> None:
        line = mod._format_source(
            {
                "kind": "arcgis-rest",
                "url": "https://svc.example/MapServer",
                "layerId": 3,
                "sourceItemId": "item-9",
                "serviceType": "MapServer",
            }
        )
        assert "item-9" in line
        assert "not a portal item" not in line


class TestKnownPortalOrigin:
    """Layers that are portal data without looking like it."""

    def test_a_published_layer_is_recognised_by_its_stamp(
        self, mod: ModuleType
    ) -> None:
        """Publishing a layer then publishing the project offered it twice."""
        layer = _Layer(
            properties={
                mod.PUBLISHED_ITEM_PROPERTY: "item-7",
                mod.PUBLISHED_LAYER_PROPERTY: "roads",
            }
        )
        assert mod._known_portal_origin(layer) == ("item-7", "roads")

    @pytest.mark.parametrize("stamp", ["", "   ", "\t\n", None, 42])
    def test_a_blank_stamp_reads_as_absent(
        self, mod: ModuleType, stamp: object
    ) -> None:
        """An empty custom property is absence, not an item id of ''.

        Asserted on ``_published_item_property`` rather than through
        ``_known_portal_origin``, because the caller happens to treat a
        falsy id as absent too and so hides whether this guard works at
        all. Found by reverting the guard and watching the outer test
        keep passing: it was checking the caller's coincidence, not the
        behaviour it named.
        """
        layer = _Layer(properties={mod.PUBLISHED_ITEM_PROPERTY: stamp})
        assert mod._published_item_property(layer) == (None, None)

    def test_a_stamped_id_is_trimmed(self, mod: ModuleType) -> None:
        """Whitespace around an id would not match anything server-side."""
        layer = _Layer(
            properties={
                mod.PUBLISHED_ITEM_PROPERTY: "  item-7  ",
                mod.PUBLISHED_LAYER_PROPERTY: "  roads  ",
            }
        )
        assert mod._published_item_property(layer) == ("item-7", "roads")

    def test_a_stamped_item_with_no_sublayer_key(self, mod: ModuleType) -> None:
        """A single-layer item names no sublayer; "" must become None."""
        layer = _Layer(
            properties={
                mod.PUBLISHED_ITEM_PROPERTY: "item-7",
                mod.PUBLISHED_LAYER_PROPERTY: "   ",
            }
        )
        assert mod._published_item_property(layer) == ("item-7", None)

    def test_an_offline_clone_is_traced_back_to_its_source(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read from the GeoPackage, so a fresh session still knows.

        A clone is a local file with the portal's own data in it.
        Offering to publish it would push that data back at the portal
        as a brand new item.
        """
        monkeypatch.setattr(
            mod,
            "read_clone_source",
            lambda path: SimpleNamespace(item_id="item-3", layer_id="roads"),
        )
        layer = _Layer(source="C:/data/clone.gpkg|layername=roads")
        assert mod._known_portal_origin(layer) == ("item-3", "roads")

    def test_the_default_alias_names_no_sublayer(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"default" is the portal's alias for a single-layer item.

        Passing it through as a real sublayer key would build a map
        source naming a layer that does not exist.
        """
        monkeypatch.setattr(
            mod,
            "read_clone_source",
            lambda path: SimpleNamespace(item_id="item-3", layer_id="default"),
        )
        layer = _Layer(source="C:/data/clone.gpkg")
        assert mod._known_portal_origin(layer) == ("item-3", None)

    def test_the_stamp_wins_over_the_geopackage(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clone that was later published names the newer item."""
        monkeypatch.setattr(
            mod,
            "read_clone_source",
            lambda path: SimpleNamespace(item_id="old", layer_id="default"),
        )
        layer = _Layer(
            source="C:/data/clone.gpkg",
            properties={mod.PUBLISHED_ITEM_PROPERTY: "new"},
        )
        assert mod._known_portal_origin(layer)[0] == "new"

    def test_an_ordinary_local_file_is_left_alone(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This runs over every layer, most of which are not ours.

        A false positive here is worse than a false negative: the map
        would point at someone else's portal item instead of offering
        to publish the user's own data.
        """
        monkeypatch.setattr(
            mod, "read_clone_source", lambda path: pytest.fail("not a gpkg")
        )
        assert mod._known_portal_origin(_Layer()) == (None, None)

    def test_a_geopackage_that_is_not_a_clone_is_left_alone(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "read_clone_source", lambda path: None)
        layer = _Layer(source="C:/data/ordinary.gpkg|layername=x")
        assert mod._known_portal_origin(layer) == (None, None)


class TestLayerOpacity:
    """Two different APIs, and a default that must not be 0."""

    def test_a_layer_with_its_own_opacity(self, mod: ModuleType) -> None:
        assert mod._layer_opacity(_Layer(opacity=0.5)) == 0.5

    def test_a_layer_whose_opacity_lives_on_its_renderer(
        self, mod: ModuleType
    ) -> None:
        assert mod._layer_opacity(_Layer(renderer_opacity=0.25)) == 0.25

    def test_a_layer_with_neither_is_fully_opaque(
        self, mod: ModuleType
    ) -> None:
        """Defaulting to 0 would publish a map of invisible layers."""
        assert mod._layer_opacity(_Layer()) == 1.0

    def test_a_raising_accessor_is_fully_opaque(
        self, mod: ModuleType
    ) -> None:
        layer = _Layer()
        layer.opacity = lambda: (_ for _ in ()).throw(RuntimeError("no"))
        assert mod._layer_opacity(layer) == 1.0


class TestTileLayerIndex:
    def test_one_bad_item_does_not_lose_the_others(
        self, mod: ModuleType
    ) -> None:
        """A single unreadable raster must not fail the whole dialog."""

        class _Items:
            def get(self, item_id: str) -> Any:
                if item_id == "bad":
                    raise RuntimeError("gone")
                return SimpleNamespace(
                    data={"tileUrl": f"https://p/{item_id}/{{z}}"},
                    bbox=[0, 0, 1, 1],
                )

        out = mod._tile_layer_index(
            SimpleNamespace(items=_Items()), {"good", "bad"}
        )
        assert set(out) == {"good"}
        assert out["good"].tile_url == "https://p/good/{z}"

    def test_an_item_without_a_tile_url_is_skipped(
        self, mod: ModuleType
    ) -> None:
        """The tile URL is the whole reason for the lookup.

        Recognising the item is not enough: the URL lives in the item's
        data envelope and nowhere in the QGIS layer.
        """
        client = SimpleNamespace(
            items=SimpleNamespace(
                get=lambda i: SimpleNamespace(data={}, bbox=None)
            )
        )
        assert mod._tile_layer_index(client, {"x"}) == {}


class TestProjectTitleFallback:
    def test_an_untitled_project_still_gets_a_name(
        self, mod: ModuleType
    ) -> None:
        """Portal items must not be created called ""."""
        assert mod._project_title_or_fallback() == "QGIS map"

    def test_a_titled_project_keeps_its_title(self, mod: ModuleType) -> None:
        _StubProject.instance()._title = "Randolph County"
        assert mod._project_title_or_fallback() == "Randolph County"


class TestCreateItems:
    """The envelopes the auto-create buttons send to the portal."""

    def _client(self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Any:
        calls: list[dict[str, Any]] = []

        def create(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(id="new")

        client = SimpleNamespace(items=SimpleNamespace(create=create))
        monkeypatch.setattr(mod, "get_client", lambda _p: client)
        return calls

    def test_a_mapserver_gets_the_map_protocol(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._client(mod, monkeypatch)
        mod._create_service_item(
            None,
            title="Roads",
            url="https://svc.example/MapServer",
            service_type="MapServer",
            layer_id=3,
        )
        assert calls[0]["data"]["protocol"] == "arcgis_map"
        assert calls[0]["data"]["selectedLayerIds"] == [3]

    def test_a_featureserver_gets_the_features_protocol(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two are not interchangeable; the portal reads them apart."""
        calls = self._client(mod, monkeypatch)
        mod._create_service_item(
            None,
            title="Roads",
            url="https://svc.example/FeatureServer",
            service_type="FeatureServer",
            layer_id=0,
        )
        assert calls[0]["data"]["protocol"] == "arcgis_features"

    def test_a_service_with_no_layer_selected_carries_none(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """layer_id 0 is a real layer; None means none was chosen.

        Treating 0 as falsy here would silently drop the first layer of
        every service, which is the one most services put first.
        """
        calls = self._client(mod, monkeypatch)
        mod._create_service_item(
            None,
            title="Roads",
            url="https://svc.example/MapServer",
            service_type="MapServer",
            layer_id=None,
        )
        assert calls[0]["data"]["selectedLayerIds"] == []
        assert calls[0]["data"]["layers"] == []

    def test_layer_zero_is_kept(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._client(mod, monkeypatch)
        mod._create_service_item(
            None,
            title="Roads",
            url="https://svc.example/MapServer",
            service_type="MapServer",
            layer_id=0,
        )
        assert calls[0]["data"]["selectedLayerIds"] == [0]
        assert calls[0]["data"]["layers"] == [{"name": "0", "title": "Roads"}]

    def test_a_basemap_carries_its_tile_template(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._client(mod, monkeypatch)
        mod._create_basemap_item(
            None, title="OSM", tile_url="https://t/{z}/{x}/{y}.png"
        )
        assert calls[0]["type"] == "basemap"
        assert calls[0]["data"]["tileUrl"] == "https://t/{z}/{x}/{y}.png"


class _TreeLayer:
    def __init__(self, layer: Any, visible: bool = True) -> None:
        self._layer = layer
        self._visible = visible

    def layer(self) -> Any:
        return self._layer

    def isVisible(self) -> bool:
        return self._visible


def _iface(
    *, center_x: float = 10.0, center_y: float = 50.0,
    scale: float = 100000.0, crs: str = "EPSG:4326",
) -> Any:
    canvas = SimpleNamespace(
        center=lambda: SimpleNamespace(
            x=lambda: center_x, y=lambda: center_y
        ),
        mapSettings=lambda: SimpleNamespace(
            destinationCrs=lambda: _StubCrs(crs)
        ),
        scale=lambda: scale,
    )
    return SimpleNamespace(mapCanvas=lambda: canvas)


class TestBuildSnapshot:
    """Reading the canvas into the shape ``translate`` consumes.

    ``translate`` is thoroughly tested and none of that helps if the
    snapshot handed to it is wrong, which is the half that lived in an
    unimported dialog module.
    """

    def _set_layers(self, tree_layers: list[Any]) -> None:
        _StubProject.instance()._tree = SimpleNamespace(
            findLayers=lambda: tree_layers
        )

    def test_layer_order_follows_the_layers_panel(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Draw order is the whole point of publishing a map.

        ``findLayers`` walks the tree top-down, and the published map
        has to keep that order or the user gets their basemap painted
        over their data.
        """
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([
            _TreeLayer(_Layer(name="Top", layer_id="a")),
            _TreeLayer(_Layer(name="Middle", layer_id="b")),
            _TreeLayer(_Layer(name="Bottom", layer_id="c")),
        ])
        snapshot = mod._build_snapshot(_iface(), "My map")
        assert [ly.name for ly in snapshot.layers] == [
            "Top", "Middle", "Bottom"
        ]

    def test_an_unloaded_tree_entry_is_skipped(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken layer path leaves a node whose layer() is None.

        Common in a project whose data moved, and it must not take the
        whole dialog down with it.
        """
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([
            _TreeLayer(None),
            _TreeLayer(_Layer(name="Real")),
        ])
        snapshot = mod._build_snapshot(_iface(), "My map")
        assert [ly.name for ly in snapshot.layers] == ["Real"]

    def test_visibility_comes_from_the_tree_not_the_layer(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unticking a layer in the panel is a tree fact, not a layer one.

        Reading it off the map layer would publish every layer visible
        regardless of what the user had turned off.
        """
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([
            _TreeLayer(_Layer(name="On"), visible=True),
            _TreeLayer(_Layer(name="Off"), visible=False),
        ])
        snapshot = mod._build_snapshot(_iface(), "My map")
        assert [ly.visible for ly in snapshot.layers] == [True, False]

    def test_a_recognised_portal_layer_carries_its_item(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([
            _TreeLayer(
                _Layer(properties={mod.PUBLISHED_ITEM_PROPERTY: "item-5"})
            )
        ])
        snapshot = mod._build_snapshot(_iface(), "My map")
        assert snapshot.layers[0].portal_item_id == "item-5"

    def test_the_centre_is_reprojected_out_of_the_project_crs(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The payload always speaks lon/lat whatever the project uses.

        Publishing from a project in a state plane CRS without this
        sends metres as degrees, which lands the map off the planet.
        """
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([])
        snapshot = mod._build_snapshot(_iface(crs="EPSG:3857"), "My map")
        assert snapshot.viewport.center_lng == 111.0
        assert snapshot.viewport.center_lat == 22.0

    def test_a_project_already_in_4326_is_not_transformed(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([])
        snapshot = mod._build_snapshot(
            _iface(center_x=-79.8, center_y=38.9), "My map"
        )
        assert snapshot.viewport.center_lng == -79.8
        assert snapshot.viewport.center_lat == 38.9

    @pytest.mark.parametrize("scale", [0.0, -1.0])
    def test_a_nonsense_scale_does_not_raise(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch, scale: float
    ) -> None:
        """log2(x/0) would be a crash on Publish, not a bad map."""
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([])
        snapshot = mod._build_snapshot(_iface(scale=scale), "My map")
        assert snapshot.viewport.zoom == 0.0

    def test_zoom_is_clamped_to_the_usable_range(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zoomed-right-in canvas must not ask for zoom 30."""
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([])
        assert mod._build_snapshot(
            _iface(scale=0.0001), "My map"
        ).viewport.zoom == 22.0

    def test_the_title_is_carried_through(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        self._set_layers([])
        assert mod._build_snapshot(_iface(), "Randolph").title == "Randolph"


class TestFetchPortalIndex:
    """Indexing the portal's basemaps and services by upstream URL.

    This is how a QGIS layer pointing at an external service gets
    traced back to the portal item it came from. Miss it and the map
    references the raw URL, which renders but is invisible to anyone
    auditing what the org depends on.
    """

    def _client(self, items: list[Any], full: dict[str, Any]) -> Any:
        return SimpleNamespace(
            items=SimpleNamespace(
                list=lambda **kw: SimpleNamespace(items=items),
                get=lambda i: full[i],
            )
        )

    def test_only_indexable_types_are_fetched_in_full(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One extra request per item, so the filter is the cost control.

        ItemSummary carries no data envelope, so indexing needs a full
        fetch. Doing that for every data_layer in a large org would
        make opening this dialog take minutes.
        """
        fetched: list[str] = []
        summaries = [
            SimpleNamespace(id="bm", type="basemap"),
            SimpleNamespace(id="svc", type="arcgis_service"),
            SimpleNamespace(id="dl", type="data_layer"),
            SimpleNamespace(id="tl", type="tile_layer"),
            SimpleNamespace(id="map", type="map"),
        ]
        full = {
            "bm": SimpleNamespace(data={"tileUrl": "https://t/{z}"}),
            "svc": SimpleNamespace(
                data={"url": "https://s.example/MapServer"}
            ),
        }

        def get(item_id: str) -> Any:
            fetched.append(item_id)
            return full[item_id]

        client = SimpleNamespace(
            items=SimpleNamespace(
                list=lambda **kw: SimpleNamespace(items=summaries), get=get
            )
        )
        monkeypatch.setattr(mod, "get_client", lambda _p: client)
        monkeypatch.setattr(mod, "_tile_layer_ids_on_canvas", lambda: set())

        mod._fetch_portal_index(None)
        assert sorted(fetched) == ["bm", "svc"]

    def test_kebab_and_snake_type_spellings_both_index(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The portal has used both spellings for the same type.

        Matching only one silently indexes nothing, and the symptom is
        a map full of untracked URLs rather than an error.
        """
        summaries = [SimpleNamespace(id="svc", type="arcgis-service")]
        full = {
            "svc": SimpleNamespace(data={"url": "https://s.example/MapServer"})
        }
        monkeypatch.setattr(
            mod, "get_client", lambda _p: self._client(summaries, full)
        )
        monkeypatch.setattr(mod, "_tile_layer_ids_on_canvas", lambda: set())

        index = mod._fetch_portal_index(None)
        assert "https://s.example/MapServer" in index.services_by_url

    def test_map_and_feature_servers_are_told_apart(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summaries = [
            SimpleNamespace(id="m", type="service"),
            SimpleNamespace(id="f", type="service"),
        ]
        full = {
            "m": SimpleNamespace(data={"url": "https://s.example/MapServer/"}),
            "f": SimpleNamespace(
                data={"url": "https://s.example/other/FeatureServer"}
            ),
        }
        monkeypatch.setattr(
            mod, "get_client", lambda _p: self._client(summaries, full)
        )
        monkeypatch.setattr(mod, "_tile_layer_ids_on_canvas", lambda: set())

        index = mod._fetch_portal_index(None)
        # The trailing slash is stripped, or no canvas URL ever matches.
        assert (
            index.services_by_url["https://s.example/MapServer"].service_type
            == "MapServer"
        )
        assert (
            index.services_by_url[
                "https://s.example/other/FeatureServer"
            ].service_type
            == "FeatureServer"
        )

    def test_an_item_with_no_usable_url_is_skipped(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summaries = [
            SimpleNamespace(id="a", type="basemap"),
            SimpleNamespace(id="b", type="service"),
        ]
        full = {
            "a": SimpleNamespace(data={}),
            "b": SimpleNamespace(data={"url": "https://s.example/Unknown"}),
        }
        monkeypatch.setattr(
            mod, "get_client", lambda _p: self._client(summaries, full)
        )
        monkeypatch.setattr(mod, "_tile_layer_ids_on_canvas", lambda: set())

        index = mod._fetch_portal_index(None)
        assert index.basemaps_by_tile_url == {}
        assert index.services_by_url == {}


class TestExclusionsAndPublish:
    """The checkbox half of #22, and why it re-translates.

    Exercised on a real ``PublishProjectDialog`` built with
    ``__new__``: the method under test touches three attributes and no
    widget tree, and constructing the whole dialog would test Qt rather
    than this. The attributes are set explicitly below, so a rename
    breaks the test rather than being silently skipped.
    """

    def _dialog(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        *, excluded: set[str], layers: list[Any],
    ) -> Any:
        monkeypatch.setattr(mod, "read_clone_source", lambda p: None)
        _StubProject.instance()._tree = SimpleNamespace(
            findLayers=lambda: layers
        )
        dialog = mod.PublishProjectDialog.__new__(mod.PublishProjectDialog)
        dialog._iface = _iface()
        dialog._excluded_layer_ids = excluded
        dialog._index = None
        dialog._title_input = SimpleNamespace(text=lambda: "My map")
        return dialog

    def test_an_unticked_layer_is_left_out_of_the_payload(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gratisgis_qgis.browser.uris import vector_tile_uri

        portal = "https://gratisgis.org"
        keep = _Layer(
            name="Keep", layer_id="keep", provider="vectortile",
            source=vector_tile_uri(portal, "item-keep"),
        )
        drop = _Layer(
            name="Drop", layer_id="drop", provider="vectortile",
            source=vector_tile_uri(portal, "item-drop"),
        )
        dialog = self._dialog(
            mod, monkeypatch,
            excluded={"drop"},
            layers=[_TreeLayer(keep), _TreeLayer(drop)],
        )
        names = [
            ly["title"] for ly in dialog._publish_translation().data["layers"]
        ]
        assert "Keep" in names
        assert "Drop" not in names

    def test_ticking_everything_publishes_everything(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gratisgis_qgis.browser.uris import vector_tile_uri

        portal = "https://gratisgis.org"
        layers = [
            _TreeLayer(
                _Layer(
                    name=f"L{i}", layer_id=str(i), provider="vectortile",
                    source=vector_tile_uri(portal, f"item-{i}"),
                )
            )
            for i in range(3)
        ]
        dialog = self._dialog(mod, monkeypatch, excluded=set(), layers=layers)
        assert len(dialog._publish_translation().data["layers"]) == 3

    def test_an_empty_layer_id_never_enters_the_exclusion_set(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard that stops one blank id hiding every unnamed layer.

        Exclusions are matched with ``lyr.qgis_layer_id or ""``, so a
        blank string in the set would exclude every layer that has no
        QGIS id at once. The set is kept clean at the point of entry
        rather than filtered at the point of use, which is why the
        check belongs on the tick handler.
        """
        dialog = self._dialog(mod, monkeypatch, excluded=set(), layers=[])
        dialog._summary_label = SimpleNamespace(setText=lambda _t: None)
        dialog._index = None

        for bad in (None, "", 42):
            item = SimpleNamespace(
                data=lambda _role, v=bad: v,
                checkState=lambda: _StubQt.CheckState.Unchecked,
            )
            dialog._on_layer_ticked(item)

        assert dialog._excluded_layer_ids == set()

    def test_unticking_a_real_layer_records_it(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And re-ticking takes it back out.

        The list is rebuilt on every index refresh, so this set is the
        only place an untick survives.
        """
        dialog = self._dialog(mod, monkeypatch, excluded=set(), layers=[])
        dialog._summary_label = SimpleNamespace(setText=lambda _t: None)

        state = {"checked": _StubQt.CheckState.Unchecked}
        item = SimpleNamespace(
            data=lambda _role: "layer-1",
            checkState=lambda: state["checked"],
        )
        dialog._on_layer_ticked(item)
        assert dialog._excluded_layer_ids == {"layer-1"}

        state["checked"] = _StubQt.CheckState.Checked
        dialog._on_layer_ticked(item)
        assert dialog._excluded_layer_ids == set()


class TestSkippedRowActions:
    def test_a_row_offers_the_action_its_kind_supports(
        self, mod: ModuleType
    ) -> None:
        service = mod.SkippedLayer(
            name="Roads", reason="not on the portal", provider="arcgismapserver",
            service_url="https://s/MapServer", service_type="MapServer",
        )
        basemap = mod.SkippedLayer(
            name="OSM", reason="not on the portal", provider="wms",
            basemap_tile_url="https://t/{z}",
        )
        local = mod.SkippedLayer(
            name="Parcels", reason="local file", provider="ogr",
            is_local_vector=True,
        )
        for skipped in (service, basemap, local):
            assert mod._has_action(skipped), skipped.name
            assert mod._SkippedRowWidget._button_label_for(skipped)

    def test_a_row_with_nothing_to_offer_gets_no_button(
        self, mod: ModuleType
    ) -> None:
        """A button that cannot do anything is worse than no button."""
        skipped = mod.SkippedLayer(
            name="Mystery", reason="unsupported", provider="memory"
        )
        assert not mod._has_action(skipped)
        assert mod._SkippedRowWidget._button_label_for(skipped) is None


class TestTileLayerIdsOnCanvas:
    def test_portal_rasters_are_found_among_ordinary_layers(
        self, mod: ModuleType
    ) -> None:
        from gratisgis_qgis.browser.uris import (
            tile_layer_cog_uri,
            tile_layer_xyz_uri,
        )

        portal = "https://gratisgis.org"
        _StubProject.instance()._layers = {
            "a": _Layer(source=tile_layer_cog_uri(portal, "item-cog")),
            "b": _Layer(source=tile_layer_xyz_uri(portal, "item-xyz")),
            "c": _Layer(source="/home/matt/local.tif"),
        }
        assert mod._tile_layer_ids_on_canvas() == {"item-cog", "item-xyz"}
