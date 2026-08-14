# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browser-tree behavior under a stubbed ``qgis.core``.

Three things carry real risk and are pinned here:

- URI selection for data_layer sublayers: public items must stay on
  the public tiles surface (anonymous project sharing), non-public
  items must use the authed per-layer MVT route when the profile has
  a layer authcfg, and every degraded case falls back to public.
- Basemap leaves must not fetch over the network in their
  constructor; the fetch belongs to the parent group's
  createChildren, which runs on the Browser worker thread precisely
  because basemap groups do not claim the Fast capability.
- The mimeUris overrides must carry the data-source URI, not the
  Browser path; sending path() is the historical "not a valid or
  recognized data source" drop bug.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import ModuleType
from typing import Any
from urllib.parse import quote

import pytest

from gratisgis_client.models.item import ItemSummary
from tests.plugin.conftest import ProfileFactory, install_qgis_stub

# ----- qgis.core stubs (module-scope so identity is stable across
# tests; browser.items binds them once on first import) -----


class _StubBase:
    def __init__(self) -> None:
        self.capabilities_v2: Any = None
        self.state: Any = None
        self.tooltip = ""

    def setCapabilitiesV2(self, caps: Any) -> None:  # QGIS API name
        self.capabilities_v2 = caps

    def setState(self, state: Any) -> None:  # QGIS API name
        self.state = state

    def setToolTip(self, text: str) -> None:  # QGIS API name
        self.tooltip = text

    def toolTip(self) -> str:  # QGIS API name
        return self.tooltip


class _StubQgsDataItem(_StubBase):
    # QGIS 3 class-level shortcuts the production resolver finds.
    Custom = 101
    Fertile = 1
    Fast = 2
    Populated = 201

    def __init__(self, *args: Any) -> None:  # (type, parent, name, path)
        super().__init__()
        self.ctor_args = args


class _StubQgsDataCollectionItem(_StubBase):
    def __init__(self, parent: Any, name: str, path: str) -> None:
        super().__init__()
        self._parent = parent
        self._name = name
        self._path = path

    def name(self) -> str:  # QGIS API name
        return self._name

    def path(self) -> str:  # QGIS API name
        return self._path


class _StubQgsLayerItem(_StubBase):
    Vector = 11
    Raster = 12
    VectorTile = 13

    def __init__(
        self,
        parent: Any,
        name: str,
        path: str,
        uri: str,
        layer_type: Any,
        provider_key: str,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._name = name
        self._path = path
        self._uri = uri
        self.layer_type = layer_type
        self.provider_key = provider_key

    def name(self) -> str:  # QGIS API name
        return self._name

    def path(self) -> str:  # QGIS API name
        return self._path

    def uri(self) -> str:  # QGIS API name
        return self._uri


class _StubMimeUri:
    def __init__(self) -> None:
        self.layerType = ""
        self.providerKey = ""
        self.uri = ""
        self.name = ""
        self.supportedCrs: list[str] = []
        self.supportedFormats: list[str] = []


class _StubMimeDataUtils:
    Uri = _StubMimeUri


class _StubQgsDataItemProvider:
    def __init__(self) -> None:
        pass


class _StubQgsDataProvider:
    Net = 8


@pytest.fixture
def items_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsDataCollectionItem": _StubQgsDataCollectionItem,
                "QgsDataItem": _StubQgsDataItem,
                "QgsLayerItem": _StubQgsLayerItem,
                "QgsMimeDataUtils": _StubMimeDataUtils,
                "QgsDataItemProvider": _StubQgsDataItemProvider,
                "QgsDataProvider": _StubQgsDataProvider,
            },
            "qgis.PyQt.QtCore": {"QSettings": type("QSettings", (), {})},
        },
    )
    import gratisgis_qgis.browser.items as items_mod

    return items_mod


_NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


def _summary(**overrides: Any) -> ItemSummary:
    base: dict[str, Any] = {
        "id": "item-1",
        "type": "data_layer",
        "title": "Parcels",
        "access": "private",
        "owner_id": "user-1",
        "org_id": "org-1",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return ItemSummary(**base)


def _v3_envelope(*layers: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"version": 3, "layers": list(layers)}}


class TestSublayerUriSelection:
    def _children(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        *,
        profile: Any,
        item: ItemSummary,
        envelope: dict[str, Any],
    ) -> list[Any]:
        monkeypatch.setattr(items_mod, "get_item", lambda _p, _i: envelope)
        node = items_mod.DataLayerItem(None, profile, item)
        return list(node.createChildren())

    def test_public_item_stays_on_public_tiles(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        # Anonymous project sharing depends on this: a project saved
        # with a public layer must keep rendering for viewers who
        # never signed in, so the authcfg never attaches to it.
        [child] = self._children(
            items_mod,
            monkeypatch,
            profile=profile_factory(layer_authcfg_id="lyr1234"),
            item=_summary(access="public"),
            envelope=_v3_envelope({"id": "roads", "geometryType": "line"}),
        )
        assert "/api/public/ogc/collections/item-1__roads/" in child.uri()
        assert "authcfg" not in child.uri()

    def test_private_item_uses_authed_route_with_authcfg(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        [child] = self._children(
            items_mod,
            monkeypatch,
            profile=profile_factory(layer_authcfg_id="lyr1234"),
            item=_summary(access="private", id="item-9"),
            envelope=_v3_envelope({"id": "roads", "geometryType": "line"}),
        )
        uri = child.uri()
        # z/x/y on the authed route; the public surface is z/y/x.
        assert "/api/items/item-9/layers/roads/tile/{z}/{x}/{y}.mvt" in uri
        assert "authcfg=lyr1234" in uri
        assert child.provider_key == "vectortile"

    def test_org_item_uses_authed_route(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        [child] = self._children(
            items_mod,
            monkeypatch,
            profile=profile_factory(layer_authcfg_id="lyr1234"),
            item=_summary(access="org"),
            envelope=_v3_envelope({"id": "roads", "geometryType": "line"}),
        )
        assert "authcfg=lyr1234" in child.uri()

    def test_private_item_without_layer_authcfg_falls_back_to_public(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        # Sign-in could not mint a key: same (empty) rendering the
        # plugin always had for private layers, never a broken URI.
        [child] = self._children(
            items_mod,
            monkeypatch,
            profile=profile_factory(layer_authcfg_id=""),
            item=_summary(access="private"),
            envelope=_v3_envelope({"id": "roads", "geometryType": "line"}),
        )
        assert "/api/public/ogc/collections/" in child.uri()
        assert "authcfg" not in child.uri()

    def test_v1_fallback_leaf_has_no_layer_id_so_stays_public(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        # Old-shape items expose no per-layer id for the authed route
        # to address; the bare-UUID public alias is the only surface.
        [child] = self._children(
            items_mod,
            monkeypatch,
            profile=profile_factory(layer_authcfg_id="lyr1234"),
            item=_summary(access="private"),
            envelope={"data": {"version": 1}},
        )
        assert "/api/public/ogc/collections/item-1/" in child.uri()
        assert "authcfg" not in child.uri()

    def test_private_table_lists_with_clone_tooltip(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        # No authed OAPIF surface exists server-side (documented
        # portal follow-up); the table stays listed but tells the
        # user which flow actually works.
        [child] = self._children(
            items_mod,
            monkeypatch,
            profile=profile_factory(layer_authcfg_id="lyr1234"),
            item=_summary(access="private"),
            envelope=_v3_envelope({"id": "lookup", "geometryType": None}),
        )
        assert child.provider_key == "OAPIF"
        assert "Clone" in child.tooltip

    def test_public_table_has_no_tooltip(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        [child] = self._children(
            items_mod,
            monkeypatch,
            profile=profile_factory(),
            item=_summary(access="public"),
            envelope=_v3_envelope({"id": "lookup", "geometryType": None}),
        )
        assert child.tooltip == ""


class TestBasemapFetchPlacement:
    def test_make_item_fetches_and_ctor_is_network_free(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        profile = profile_factory()
        item = _summary(type="basemap", access="public")
        fetched: list[str] = []
        tile_url = "https://tiles.example/{z}/{y}/{x}.png"

        def fake_get_item(_profile: Any, item_id: str) -> dict[str, Any]:
            fetched.append(item_id)
            return {"data": {"kind": "tile-url", "tileUrl": tile_url}}

        monkeypatch.setattr(items_mod, "get_item", fake_get_item)
        parent = _StubQgsDataCollectionItem(None, "Basemaps", "gratisgis:/demo/mine")
        child = items_mod._make_item(parent, profile, item)

        assert isinstance(child, items_mod.BasemapItem)
        # The fetch happened in _make_item (the parent's
        # createChildren context), exactly once.
        assert fetched == [item.id]
        assert quote(tile_url, safe="") in child.uri()

    def test_basemap_ctor_takes_data_without_fetching(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        def explode(_profile: Any, _item_id: str) -> None:
            raise AssertionError("BasemapItem constructor must not fetch")

        monkeypatch.setattr(items_mod, "get_item", explode)
        node = items_mod.BasemapItem(
            None,
            profile_factory(),
            _summary(type="basemap", access="public"),
            data={"tileUrl": "https://tiles.example/{z}/{y}/{x}.png"},
        )
        assert "tiles.example" in node.uri()

    def test_basemap_with_no_data_yields_empty_uri(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        node = items_mod.BasemapItem(
            None, profile_factory(), _summary(type="basemap"), data=None
        )
        assert node.uri() == ""

    def test_basemap_group_is_not_fast_but_others_are(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        # Fast means "createChildren runs on the GUI thread"; basemap
        # groups fetch per-item data in createChildren, so claiming
        # Fast there would move those HTTP calls onto the GUI thread.
        parent = _StubQgsDataCollectionItem(None, "bucket", "gratisgis:/demo/mine")
        profile = profile_factory()
        basemaps = items_mod._TypeGroupItem(
            parent, profile, type_key="basemap", items=[]
        )
        data_layers = items_mod._TypeGroupItem(
            parent, profile, type_key="data_layer", items=[]
        )
        fast = items_mod._BROWSER_CAP_FAST
        assert not basemaps.capabilities_v2 & fast
        assert data_layers.capabilities_v2 & fast


class TestMimeUris:
    def test_tile_layer_item_mime_uri_uses_data_source_uri(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        node = items_mod.TileLayerItem(
            None, profile_factory(), _summary(type="tile_layer", title="Tiles")
        )
        [mime] = node.mimeUris()
        # A tile_layer is a RASTER pyramid (COG), not vector tiles.
        # This asserted vector-tile until 0.2.3, which is exactly why
        # every tile_layer added a layer that drew nothing: the
        # vector-tile collection it named does not exist for these
        # items.
        assert mime.layerType == "raster"
        assert mime.providerKey == "gdal"
        assert mime.name == "Tiles"
        # The historical drop bug: path() in the mime payload reads
        # as "not a valid or recognized data source" in QGIS.
        assert mime.uri == node.uri()
        assert mime.uri != node.path()

    def test_tile_layer_uri_is_a_vsicurl_cog(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        profile = profile_factory(portal_url="https://portal.test")
        node = items_mod.TileLayerItem(
            None, profile, _summary(type="tile_layer", title="Tiles")
        )
        assert node.uri().startswith("/vsicurl/https://portal.test/api/tile-layer/")
        assert node.uri().endswith("/file.cog")
        # The portal's own cog:// protocol is a browser-side handler;
        # letting it reach QGIS is what produced the silent empty layer.
        assert "cog://" not in node.uri()


class TestProviderCapabilities:
    def test_net_capability_resolves_via_stub(
        self, items_mod: ModuleType
    ) -> None:
        # items_mod fixture installs the full stub set provider.py
        # needs; importing it binds _NET_CAPABILITY through the same
        # resolver chain production uses.
        import gratisgis_qgis.browser.provider as provider_mod

        assert _StubQgsDataProvider.Net == provider_mod._NET_CAPABILITY


class TestTileLayerRouting:
    """tile_layer items are rasters, and only some formats can open.

    Before 0.2.3 every tile_layer was built as a vector-tile layer
    pointed at the portal's public OGC collections, which do not exist
    for these items, so each one added a layer that drew nothing and
    reported no error. These tests pin the format-driven routing.
    """

    def _item(self) -> ItemSummary:
        return _summary(type="tile_layer", title="Hillshade", id="tl-1")

    def test_cog_becomes_a_gdal_raster_layer(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {"data": {"format": "cog", "processingState": "ready"}},
        )
        child = items_mod._make_item(None, profile_factory(), self._item())
        assert isinstance(child, items_mod.TileLayerItem)
        assert child.uri().endswith("/file.cog")

    def test_pmtiles_uses_the_portal_xyz_route(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # GDAL cannot open a raster PMTiles archive, so the portal
        # unpacks tiles server-side (v0.9.26) and the plugin points at
        # that route. XYZ is the right shape because QGIS applies an
        # authcfg to XYZ requests, unlike a GDAL source.
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {
                "data": {
                    "format": "pmtiles",
                    "processingState": "ready",
                    "minZoom": 3,
                    "maxZoom": 17,
                }
            },
        )
        profile = profile_factory(
            portal_url="https://portal.test", layer_authcfg_id="lyr1234"
        )
        child = items_mod._make_item(None, profile, self._item())
        assert isinstance(child, items_mod.PmtilesTileLayerItem)
        uri = child.uri()
        assert uri.startswith("type=xyz&url=")
        assert quote("/api/tile-layer/tl-1/tiles/{z}/{x}/{y}.png", safe="") in uri
        assert "zmin=3" in uri and "zmax=17" in uri
        assert "authcfg=lyr1234" in uri

    def test_public_pmtiles_carries_no_authcfg(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same rule the vector sublayers follow: a project saved with a
        # public layer must keep rendering for viewers who never
        # signed in.
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {"data": {"format": "pmtiles", "processingState": "ready"}},
        )
        child = items_mod._make_item(
            None,
            profile_factory(layer_authcfg_id="lyr1234"),
            _summary(type="tile_layer", id="tl-1", access="public"),
        )
        assert "authcfg" not in child.uri()

    def test_unknown_format_is_surfaced_but_not_draggable(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {"data": {"format": "mbtiles", "processingState": "ready"}},
        )
        child = items_mod._make_item(None, profile_factory(), self._item())
        assert isinstance(child, items_mod.UnsupportedTileLayerItem)

    @pytest.mark.parametrize(
        "fmt,state,expected",
        [
            # The regression that hid every PMTiles layer: a finished
            # pyramid reports 'pmtiles-ready', not 'ready', and an
            # earlier readiness gate on state == 'ready' therefore
            # routed all six of them to the "still preparing" row.
            ("pmtiles", "pmtiles-ready", "PmtilesTileLayerItem"),
            ("cog", "ready", "TileLayerItem"),
            # Every other state still serves a file per the portal's
            # own state machine, so none of them may block adding.
            ("cog", "cog-ready", "TileLayerItem"),
            ("cog", "tiling", "TileLayerItem"),
            ("cog", "tiling-failed", "TileLayerItem"),
            ("cog", "building", "TileLayerItem"),
            ("pmtiles", "failed", "PmtilesTileLayerItem"),
        ],
    )
    def test_format_decides_the_provider_not_the_state(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
        fmt: str,
        state: str,
        expected: str,
    ) -> None:
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {"data": {"format": fmt, "processingState": state}},
        )
        child = items_mod._make_item(None, profile_factory(), self._item())
        assert type(child).__name__ == expected

    def test_no_format_yet_is_not_drawable(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Nothing is being served yet, which is the one case worth
        # blocking on.
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {"data": {"processingState": "uploading"}},
        )
        child = items_mod._make_item(None, profile_factory(), self._item())
        assert isinstance(child, items_mod.UnsupportedTileLayerItem)
        assert "uploading" in child.tooltip

    def test_tile_layer_group_does_not_claim_fast(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        # Each child needs its data envelope fetched, so the group must
        # stay on the Browser worker thread rather than the GUI thread.
        parent = _StubQgsDataCollectionItem(None, "bucket", "gratisgis:/demo/mine")
        group = items_mod._TypeGroupItem(
            parent, profile_factory(), type_key="tile_layer", items=[]
        )
        assert not group.capabilities_v2 & items_mod._BROWSER_CAP_FAST


class TestLayerTargetResolution:
    """The search dock must route exactly like the Browser tree.

    It used to build its own URIs for two item types only, which
    drifted: data layers went to the public-only OAPIF surface (so
    private ones came back empty), tile layers to the vector-tile
    surface that does not exist for them, and basemaps were refused
    outright even though the tree adds them. Resolving through the
    tree's own leaf is what stops the two from disagreeing again.
    """

    def _resolve(self, items_mod: ModuleType, profile: Any, summary: ItemSummary) -> Any:
        # Resolution lives outside ui/ precisely so it can be
        # exercised without Qt widgets; import after the qgis stub.
        from gratisgis_qgis.layer_targets import resolve_layer_target

        return resolve_layer_target(profile, summary)

    def test_basemap_resolves_to_a_raster_target(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {"data": {"tileUrl": "https://tiles.test/{z}/{x}/{y}.png"}},
        )
        target = self._resolve(
            items_mod,
            profile_factory(),
            _summary(type="basemap", title="Open Street Map", id="bm-1"),
        )
        assert target is not None
        assert target.layer_type == "raster"
        assert target.provider == "wms"
        assert "tiles.test" in target.uri
        assert target.name == "Open Street Map"

    def test_pmtiles_tile_layer_resolves_to_the_xyz_route(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {
                "data": {"format": "pmtiles", "processingState": "pmtiles-ready"}
            },
        )
        target = self._resolve(
            items_mod,
            profile_factory(layer_authcfg_id="lyr1234"),
            _summary(type="tile_layer", title="Hillshade", id="tl-1"),
        )
        assert target is not None
        assert target.layer_type == "raster"
        assert "type=xyz" in target.uri
        assert "authcfg=lyr1234" in target.uri

    def test_private_data_layer_resolves_to_the_authed_route(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The old dock sent this to public OAPIF, so a private layer
        # added from search silently drew nothing.
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: _v3_envelope({"id": "roads", "geometryType": "line"}),
        )
        target = self._resolve(
            items_mod,
            profile_factory(layer_authcfg_id="lyr1234"),
            _summary(type="data_layer", access="private", id="item-9"),
        )
        assert target is not None
        assert "authcfg=lyr1234" in target.uri
        assert target.layer_type == "vector-tile"

    def test_not_drawable_item_reports_its_reason(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            items_mod,
            "get_item",
            lambda _p, _i: {"data": {"processingState": "uploading"}},
        )
        target = self._resolve(
            items_mod,
            profile_factory(),
            _summary(type="tile_layer", id="tl-2"),
        )
        assert target is not None
        assert not target.uri
        assert "uploading" in target.message
