# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sharing from the plugin (#13): the decisions, not the radio wiring.

What is worth pinning: when a save actually happens, how a refusal
reads, and that every portal leaf in the tree offers the action. The
dialog itself is three radios around these answers.
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.plugin.conftest import ProfileFactory, install_qgis_stub
from tests.plugin.test_browser_items import _summary


@pytest.fixture
def sharing_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.PyQt.QtWidgets": {
                name: type(name, (), {})
                for name in (
                    "QCheckBox", "QDialog", "QDialogButtonBox", "QLabel",
                    "QMessageBox", "QRadioButton", "QVBoxLayout", "QWidget",
                )
            },
            "qgis.PyQt.QtCore": {"QSettings": type("QSettings", (), {})},
        },
    )
    import gratisgis_qgis.ui.sharing_dialog as mod

    return mod


class TestPlanSharingChange:
    def test_a_real_change_is_saved(self, sharing_mod: Any) -> None:
        assert sharing_mod.plan_sharing_change("private", "org") == "org"
        assert sharing_mod.plan_sharing_change("public", "private") == "private"

    def test_no_change_saves_nothing(self, sharing_mod: Any) -> None:
        """A PATCH writing the same value still bumps the updated
        stamp, which reorders recency lists. Doing nothing must
        actually do nothing."""
        assert sharing_mod.plan_sharing_change("org", "org") is None

    def test_an_unknown_value_is_never_sent(self, sharing_mod: Any) -> None:
        assert sharing_mod.plan_sharing_change("private", "everyone!!") is None

    def test_the_choices_cover_the_portal_vocabulary(
        self, sharing_mod: Any
    ) -> None:
        values = [v for v, _l, _d in sharing_mod.SHARING_CHOICES]
        assert values == ["private", "org", "public"]


class TestSharingErrorText:
    def test_a_403_reads_as_an_ownership_sentence(
        self, sharing_mod: Any
    ) -> None:
        text = sharing_mod.sharing_error_text(RuntimeError("HTTP 403 Forbidden"))
        assert "owner" in text
        assert "403" not in text, "status codes are not for humans"

    def test_other_failures_keep_their_wording(self, sharing_mod: Any) -> None:
        text = sharing_mod.sharing_error_text(RuntimeError("HTTP 500 boom"))
        assert "boom" in text or "500" in text


class TestSharingDialogChoice:
    """The one decision the dialog makes itself: which radio wins."""

    class _Radio:
        def __init__(self, checked: bool) -> None:
            self._checked = checked

        def isChecked(self) -> bool:  # Qt API name
            return self._checked

    def _dialog(self, sharing_mod: Any, checked: str, current: str) -> Any:
        dlg = sharing_mod.SharingDialog.__new__(sharing_mod.SharingDialog)
        dlg._current = current
        dlg._radios = [
            (value, self._Radio(value == checked))
            for value, _label, _desc in sharing_mod.SHARING_CHOICES
        ]
        return dlg

    def test_the_checked_radio_is_the_choice(self, sharing_mod: Any) -> None:
        dlg = self._dialog(sharing_mod, checked="public", current="private")
        assert dlg._chosen() == "public"

    def test_no_checked_radio_falls_back_to_current(
        self, sharing_mod: Any
    ) -> None:
        """Impossible through the UI, but the fallback keeps a broken
        state from silently publishing someone's private item."""
        dlg = self._dialog(sharing_mod, checked="none-of-them", current="org")
        assert dlg._chosen() == "org"


class TestEveryLeafOffersSharing:
    """One builder, six leaf classes: the menu must not depend on
    which item type happens to be right-clicked."""

    def test_the_action_is_offered_by_every_portal_leaf(
        self,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        from tests.plugin.test_browser_items import (
            _StubMimeDataUtils,
            _StubQgsDataCollectionItem,
            _StubQgsDataItem,
            _StubQgsDataItemProvider,
            _StubQgsDataProvider,
            _StubQgsLayerItem,
        )

        recorded: list[str] = []

        class _Action:
            def __init__(self, text: str, _parent: Any) -> None:
                recorded.append(text)
                self.triggered = type(
                    "S", (), {"connect": staticmethod(lambda _s: None)}
                )()

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
                "qgis.PyQt.QtWidgets": {"QAction": _Action},
            },
        )
        import gratisgis_qgis.browser.items as items_mod

        profile = profile_factory()
        monkeypatch.setattr(
            items_mod, "get_item", lambda _p, _i: {"data": {"format": "cog"}}
        )
        leaves: list[Any] = [
            items_mod.DataLayerItem(None, profile, _summary(type="data_layer")),
            items_mod.TileLayerItem(
                None, profile, _summary(type="tile_layer"), data={}
            ),
            items_mod.BasemapItem(
                None, profile, _summary(type="basemap"),
                data={"tileUrl": "https://t/{z}/{x}/{y}.png"},
            ),
            items_mod.ServiceItem(None, profile, _summary(type="service")),
            items_mod.GenericItem(None, profile, _summary(type="form")),
            items_mod.MapItem(None, profile, _summary(type="map")),
        ]
        for leaf in leaves:
            recorded.clear()
            leaf.actions(None)
            assert "Sharing..." in recorded, (
                f"{type(leaf).__name__} offers no sharing action"
            )


class TestGroupShareDiff:
    def test_only_the_changed_boxes_become_calls(self, sharing_mod: Any) -> None:
        """An untouched group's share row carries admin-set geographic
        limits; rewriting it would churn them."""
        to_share, to_unshare = sharing_mod.plan_group_share_changes(
            ["g-1", "g-2"], ["g-2", "g-3"]
        )
        assert to_share == ["g-3"]
        assert to_unshare == ["g-1"]

    def test_no_change_means_no_calls(self, sharing_mod: Any) -> None:
        assert sharing_mod.plan_group_share_changes(["g-1"], ["g-1"]) == ([], [])

    def test_starting_from_nothing_shares_everything_chosen(
        self, sharing_mod: Any
    ) -> None:
        assert sharing_mod.plan_group_share_changes([], ["b", "a"]) == (
            ["a", "b"],
            [],
        )


class TestSharingDialogGroups:
    class _Box:
        def __init__(self, checked: bool) -> None:
            self._checked = checked

        def isChecked(self) -> bool:  # Qt API name
            return self._checked

    def test_chosen_groups_reads_the_checked_boxes(
        self, sharing_mod: Any
    ) -> None:
        dlg = sharing_mod.SharingDialog.__new__(sharing_mod.SharingDialog)
        dlg._group_boxes = [
            ("g-1", self._Box(True)),
            ("g-2", self._Box(False)),
            ("g-3", self._Box(True)),
        ]
        assert dlg._chosen_groups() == ["g-1", "g-3"]

    def test_a_dialog_without_a_group_section_chooses_none(
        self, sharing_mod: Any
    ) -> None:
        """The fetch-failure fallback opens without the section, and
        saving from it must not unshare anything."""
        dlg = sharing_mod.SharingDialog.__new__(sharing_mod.SharingDialog)
        dlg._group_boxes = []
        assert dlg._chosen_groups() == []


class TestClientGroupSurface:
    """The wire shapes the sharing dialog depends on."""

    def test_group_shares_are_read_off_the_item_payload(self) -> None:
        from gratisgis_client.endpoints.items import ItemsEndpoint

        class _Http:
            def request_json(self, method: str, path: str, **_kw: Any) -> Any:
                assert (method, path) == ("GET", "/items/i-1")
                return {
                    "id": "i-1",
                    "shares": [
                        {"principalType": "group", "principalId": "g-1"},
                        {"principalType": "user", "principalId": "u-9"},
                        {"principalType": "group", "principalId": "g-1"},
                        {"principalType": "group", "principalId": "g-2"},
                    ],
                }

        endpoint = ItemsEndpoint(_Http())  # type: ignore[arg-type]
        assert endpoint.list_group_shares("i-1") == ["g-1", "g-2"]

    def test_share_and_unshare_speak_the_portal_dto(self) -> None:
        from gratisgis_client.endpoints.items import ItemsEndpoint

        calls: list[tuple[str, str, dict[str, Any]]] = []

        class _Http:
            def request_json(self, method: str, path: str, **kw: Any) -> Any:
                calls.append((method, path, kw.get("json") or {}))
                return {}

        endpoint = ItemsEndpoint(_Http())  # type: ignore[arg-type]
        endpoint.share_with_group("i-1", "g-1")
        endpoint.unshare_with_group("i-1", "g-1")
        assert calls[0] == (
            "POST",
            "/items/i-1/share",
            {"principalType": "group", "principalId": "g-1",
             "permission": "view"},
        )
        assert calls[1] == (
            "DELETE",
            "/items/i-1/share",
            {"principalType": "group", "principalId": "g-1"},
        )

    def test_the_group_list_tolerates_junk_rows(self) -> None:
        from gratisgis_client.endpoints.groups import GroupsEndpoint

        class _Http:
            def request_json(self, _m: str, _p: str, **_kw: Any) -> Any:
                return [
                    {"id": "g-1", "name": "Field crew"},
                    {"name": "no id"},
                    "not a dict",
                    {"id": "g-2"},
                ]

        groups = GroupsEndpoint(_Http()).list()  # type: ignore[arg-type]
        assert [(g.id, g.name) for g in groups] == [
            ("g-1", "Field crew"),
            ("g-2", "g-2"),
        ]
