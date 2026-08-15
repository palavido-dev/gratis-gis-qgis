# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Browser tree's remaining nodes: root, services, and the fallback.

``RootItem``, ``ServiceItem`` and ``GenericItem`` had never been
constructed by anything, which is how ``ConnectionItem`` shipped a bug
that offered private layers to a signed-out user.

The service node is the interesting one. A connected service wraps an
ArcGIS REST endpoint that may host fifty sublayers, and the URI a
sublayer needs depends on which kind of server it is in a way that is
not guessable: a FeatureServer layer is a real endpoint, while a
MapServer layer is a filter on the parent's rendered image. Getting it
backwards produces "Network error: Invalid URL", which reads as a
broken service rather than a plugin bug.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import ModuleType
from typing import Any

import pytest

from gratisgis_client.models.item import ItemSummary
from tests.plugin.conftest import ProfileFactory, install_qgis_stub
from tests.plugin.test_browser_items import (
    _StubMimeDataUtils,
    _StubQgsDataCollectionItem,
    _StubQgsDataItem,
    _StubQgsDataItemProvider,
    _StubQgsDataProvider,
    _StubQgsLayerItem,
)


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
    import gratisgis_qgis.browser.items as m

    return m


class _Store:
    def __init__(self, profiles: dict[str, Any]) -> None:
        self.profiles = dict(profiles)

    def list_names(self) -> list[str]:
        return sorted(self.profiles)

    def get(self, name: str) -> Any:
        return self.profiles.get(name)


def _labels(children: list[Any]) -> list[str]:
    out = []
    for child in children:
        args = getattr(child, "ctor_args", None)
        out.append(str(args[2]) if args else str(getattr(child, "_name", "")))
    return out


_NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


def _service_item() -> ItemSummary:
    return ItemSummary(
        id="svc-1",
        type="service",
        title="County services",
        access="org",
        owner_id="user-1",
        org_id="org-1",
        created_at=_NOW,
        updated_at=_NOW,
    )


class TestRootItem:
    def test_a_fresh_install_says_how_to_start(
        self, items_mod: ModuleType
    ) -> None:
        """The very first thing a new user sees in the panel.

        An empty node with no explanation reads as a broken plugin.
        """
        node = items_mod.RootItem(None, _Store({}))
        [child] = node.createChildren()
        assert "connection" in _labels([child])[0].lower()

    def test_every_configured_connection_gets_a_row(
        self, items_mod: ModuleType, profile_factory: ProfileFactory
    ) -> None:
        store = _Store({
            "alpha": profile_factory(name="alpha"),
            "beta": profile_factory(name="beta"),
        })
        assert len(items_mod.RootItem(None, store).createChildren()) == 2

    def test_a_connection_that_vanished_mid_walk_is_skipped(
        self, items_mod: ModuleType
    ) -> None:
        """list_names and get are two reads of a store the user can edit.

        The connection manager is a separate dialog, so a delete can
        land between them. Skipping is right; raising would take the
        whole panel down over one removed connection.
        """

        class _Racy(_Store):
            def get(self, name: str) -> Any:
                return None

        node = items_mod.RootItem(None, _Racy({"alpha": object()}))
        [child] = node.createChildren()
        assert "connection" in _labels([child])[0].lower()


class TestServiceItem:
    def _children(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile: Any,
        envelope: Any,
    ) -> list[Any]:
        monkeypatch.setattr(items_mod, "get_item", lambda _p, _i: envelope)
        node = items_mod.ServiceItem(None, profile, _service_item())
        return list(node.createChildren())

    def test_a_service_with_no_url_says_so(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """And names where to fix it, since the fix is not in QGIS."""
        children = self._children(
            items_mod, monkeypatch, profile_factory(), {"data": {}}
        )
        assert "portal" in _labels(children)[0].lower()

    def test_each_sublayer_becomes_a_leaf(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        children = self._children(
            items_mod, monkeypatch, profile_factory(),
            {
                "data": {
                    "url": "https://svc.example/rest/services/X/MapServer",
                    "layers": [
                        {"name": "0", "title": "Counties"},
                        {"name": "1", "title": "Roads"},
                    ],
                }
            },
        )
        assert _labels(children) == ["Counties", "Roads"]

    def test_a_sublayer_with_no_id_is_skipped(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """It has no addressable URL, so a leaf could only fail.

        Layer id "0" is a real id and must survive: a falsy check here
        would drop the first layer of every service.
        """
        children = self._children(
            items_mod, monkeypatch, profile_factory(),
            {
                "data": {
                    "url": "https://svc.example/X/MapServer",
                    "layers": [
                        {"name": "0", "title": "Counties"},
                        {"name": "", "title": "Broken"},
                        {"title": "Also broken"},
                    ],
                }
            },
        )
        assert _labels(children) == ["Counties"]

    def test_an_unprobed_service_falls_back_to_one_leaf(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """Better than an empty node for a service that does work."""
        children = self._children(
            items_mod, monkeypatch, profile_factory(),
            {"data": {"url": "https://svc.example/X/MapServer"}},
        )
        assert len(children) == 1
        assert _labels(children) == ["County services"]

    def test_a_mapserver_sublayer_points_at_the_service_root(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """MapServer/N is not independently fetchable.

        Only the parent's /export renders, filtered by a layers= key.
        Pointing the provider at the leaf URL gives "Network error:
        Invalid URL", because QGIS appends /export to a path the server
        will not accept.
        """
        [child] = self._children(
            items_mod, monkeypatch, profile_factory(),
            {
                "data": {
                    "url": "https://svc.example/X/MapServer",
                    "layers": [{"name": "3", "title": "Roads"}],
                }
            },
        )
        uri = child.uri()
        assert "show:3" in uri
        assert "MapServer/3" not in uri
        assert child.provider_key == "arcgismapserver"

    def test_a_featureserver_sublayer_points_at_the_layer_itself(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """The mirror image, and the reason the two cannot share code."""
        [child] = self._children(
            items_mod, monkeypatch, profile_factory(),
            {
                "data": {
                    "url": "https://svc.example/X/FeatureServer",
                    "layers": [{"name": "3", "title": "Roads"}],
                }
            },
        )
        uri = child.uri()
        assert "FeatureServer/3" in uri
        assert "show:3" not in uri
        assert child.provider_key == "arcgisfeatureserver"

    def test_a_whole_service_leaf_filters_nothing(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """No sublayer metadata means no filter to apply.

        Emitting "show:None" would be worse than emitting nothing.
        """
        [child] = self._children(
            items_mod, monkeypatch, profile_factory(),
            {"data": {"url": "https://svc.example/X/MapServer"}},
        )
        assert "show:" not in child.uri()
        assert "None" not in child.uri()

    def test_a_layer_without_a_title_falls_back_to_its_id(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """A blank row in the tree cannot be told apart from its siblings."""
        children = self._children(
            items_mod, monkeypatch, profile_factory(),
            {
                "data": {
                    "url": "https://svc.example/X/MapServer",
                    "layers": [{"name": "7"}],
                }
            },
        )
        assert _labels(children) == ["7"]

    def test_an_unreadable_item_does_not_take_the_tree_down(
        self, items_mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """get_item returns None on a forbidden or deleted item."""
        children = self._children(
            items_mod, monkeypatch, profile_factory(), None
        )
        assert len(children) == 1
        assert "portal" in _labels(children)[0].lower()


class TestGenericItem:
    def test_an_unknown_item_type_still_renders_a_row(
        self, items_mod: ModuleType, profile_factory: ProfileFactory
    ) -> None:
        """The portal grows types faster than the plugin learns them.

        A type nobody has taught the tree about should show up as an
        inert row rather than vanish, so the user can see it exists and
        open it in the portal.
        """
        item = ItemSummary(
            id="x-1",
            type="some_future_type",
            title="Mystery",
            access="private",
            owner_id="user-1",
            org_id="org-1",
            created_at=_NOW,
            updated_at=_NOW,
        )
        node = items_mod.GenericItem(None, profile_factory(), item)
        # QgsDataItem(type, parent, name, path); the stub keeps them.
        _type, _parent, label, path = node.ctor_args
        assert "Mystery" in label
        assert "some_future_type" in label, (
            "the row has to say what it is, or it reads as a broken layer"
        )
        assert path.endswith("/x-1")

    def test_it_is_marked_populated_so_it_shows_no_expander(
        self, items_mod: ModuleType, profile_factory: ProfileFactory
    ) -> None:
        """An expander that opens onto nothing is a dead end.

        The row exists to say "this item is here, open it in the
        portal", and a twisty arrow promises children it does not have.
        """
        item = ItemSummary(
            id="x-1",
            type="dashboard",
            title="Ops board",
            access="private",
            owner_id="user-1",
            org_id="org-1",
            created_at=_NOW,
            updated_at=_NOW,
        )
        node = items_mod.GenericItem(None, profile_factory(), item)
        assert node.state == items_mod._POPULATED_STATE
