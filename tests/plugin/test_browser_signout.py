# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Browser tree after the user signs out.

The bug these exist for: signing out left every private layer still
listed and still offerable, because ``ConnectionItem`` and
``BucketItem`` captured a ``ConnectionProfile`` when they were built
and never looked at the store again. A profile is a frozen snapshot,
so the node went on describing a connection that was signed in
minutes ago.

Refreshing did not help, and that is the part worth understanding.
QGIS's refresh calls ``createChildren()`` and then reconciles the
result against the children already on screen, keeping any existing
node whose name and path still match instead of swapping in the newly
built one. Signing out changes neither the name nor the path of a
connection node, so QGIS kept the stale object and discarded the fresh
one. An item's path is its identity; state that changes without the
path changing does not survive a refresh, it defeats it.

So the assertion throughout is not "a signed-out profile yields
signed-out rows", which the old code would also have passed when
handed a fresh profile. It is that a node built while signed IN starts
reporting signed-out once the store changes underneath it. That is the
only shape of test the old code fails.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import ProfileFactory, install_qgis_stub
from tests.plugin.test_browser_items import (  # reuse the stub classes
    _StubMimeDataUtils,
    _StubQgsDataCollectionItem,
    _StubQgsDataItem,
    _StubQgsDataItemProvider,
    _StubQgsDataProvider,
    _StubQgsLayerItem,
)


class FakeStore:
    """A connection store whose contents can change under a live node.

    The whole point is that this is mutable while the item that reads
    it stays alive, which is exactly what signing out does to a tree
    node that is already on screen.
    """

    def __init__(self, profiles: dict[str, Any]) -> None:
        self.profiles = dict(profiles)
        self.get_calls = 0

    def list_names(self) -> list[str]:
        return sorted(self.profiles)

    def get(self, name: str) -> Any:
        self.get_calls += 1
        return self.profiles.get(name)


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


def _labels(children: list[Any]) -> list[str]:
    """The text each row shows, whichever kind of node it is.

    Message rows are ``QgsDataItem(type, parent, name, path)`` and land
    in the stub's ``ctor_args``; collection rows keep their name on the
    instance. Reading both means a test can assert on what the user
    sees without caring which class produced it.
    """
    out = []
    for child in children:
        args = getattr(child, "ctor_args", None)
        if args:
            out.append(str(args[2]))
        else:
            out.append(str(getattr(child, "_name", "")))
    return out


class TestConnectionItemAfterSignOut:
    def test_a_live_node_stops_offering_buckets_once_signed_out(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        """The regression, stated exactly.

        Build the node while signed in, as the user's tree was. Then
        sign out in the store, without rebuilding the node, because
        QGIS's refresh does not rebuild it. The node must now report
        signed-out rather than four buckets of private content.
        """
        signed_in = profile_factory(authcfg_id="auth-1", oidc_issuer="https://i")
        store = FakeStore({"demo": signed_in})
        node = items_mod.ConnectionItem(None, store, "demo")

        assert len(node.createChildren()) == 4, "signed in: four buckets"

        store.profiles["demo"] = profile_factory(
            authcfg_id="", layer_authcfg_id="", oidc_issuer="https://i"
        )
        children = node.createChildren()

        assert len(children) == 1
        assert "sign in" in _labels(children)[0].lower()

    def test_the_node_holds_no_profile_of_its_own(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        """Nothing may cache the profile between expands.

        A node that answered from a cached copy would pass the test
        above if it happened to refresh at the right moment, and fail
        for the user. Reading the store every time is the property that
        makes the behaviour independent of when QGIS chooses to call.
        """
        store = FakeStore({"demo": profile_factory(authcfg_id="auth-1")})
        node = items_mod.ConnectionItem(None, store, "demo")
        before = store.get_calls
        node.createChildren()
        node.createChildren()
        assert store.get_calls > before + 1, "each expand must re-read the store"

    def test_a_deleted_connection_says_so(
        self,
        items_mod: ModuleType,
        profile_factory: ProfileFactory,
    ) -> None:
        """Deleting a connection is the same class of change as signing out.

        The dialog that deletes it is separate from the tree, so the
        node outlives the profile and must not raise on the way out.
        """
        store = FakeStore({"demo": profile_factory(authcfg_id="auth-1")})
        node = items_mod.ConnectionItem(None, store, "demo")
        del store.profiles["demo"]
        children = node.createChildren()
        assert len(children) == 1
        assert "deleted" in _labels(children)[0].lower()


class TestBucketItemAfterSignOut:
    def test_a_live_bucket_stops_listing_items_once_signed_out(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """The bucket is where the private titles were actually shown.

        Fixing only ``ConnectionItem`` would leave these nodes alive
        under it, still holding the signed-in profile, because QGIS
        keeps them across a refresh for the same reason.
        """
        store = FakeStore({"demo": profile_factory(authcfg_id="auth-1")})
        node = items_mod.BucketItem(None, store, "demo", kind="mine")

        called: list[Any] = []

        def record(profile: Any) -> list[Any]:
            called.append(profile)
            return []

        monkeypatch.setattr(items_mod, "list_items", record)

        node.createChildren()
        assert called, "signed in: the bucket queries the portal"

        called.clear()
        store.profiles["demo"] = profile_factory(authcfg_id="")
        children = node.createChildren()

        assert not called, "signed out: the portal must not be queried at all"
        assert _labels(children) == ["Not signed in."]

    def test_signed_out_is_a_state_not_an_error(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """Plain wording, and no exception path.

        Before the fix this surfaced as "Failed to load: Not signed in",
        a red error row produced by letting the client raise. Signing
        out is something the user did on purpose; it should read like a
        state the tree is in, not like the plugin broke.
        """
        store = FakeStore({"demo": profile_factory(authcfg_id="")})
        node = items_mod.BucketItem(None, store, "demo", kind="mine")

        def explode(_profile: Any) -> list[Any]:
            raise AssertionError("list_items must not be reached when signed out")

        monkeypatch.setattr(items_mod, "list_items", explode)
        [child] = node.createChildren()
        label = _labels([child])[0]
        assert "Failed" not in label
        assert label == "Not signed in."

    def test_a_deleted_connection_does_not_query_either(
        self,
        items_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        store = FakeStore({})
        node = items_mod.BucketItem(None, store, "demo", kind="mine")
        monkeypatch.setattr(
            items_mod,
            "list_items",
            lambda _p: (_ for _ in ()).throw(AssertionError("must not query")),
        )
        assert _labels(node.createChildren()) == ["Not signed in."]
