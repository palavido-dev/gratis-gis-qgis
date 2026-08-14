# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which project layers each dialog offers.

This is where the feature was broken while the tests were green. Both
dialogs decided a layer was portal-backed by parsing it as OAPIF, but
the Browser tree only emits OAPIF for NON-SPATIAL sublayers; ordinary
spatial data arrives as a vector-tile layer. Every test here therefore
builds its layer sources by calling the real URI builders, so the
picker logic is exercised against what the tree actually produces.

The dialogs' ``_populate_layer_combo`` runs unbound against a
stand-in ``self``. Constructing the real QDialog would need a Qt
widget tree the CI runner has no bindings for, and stubbing that
deeply tests the stubs; calling the production method with a fake
combo tests the production method.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from gratisgis_qgis.browser.uris import (
    authed_vector_tile_uri,
    oapif_uri,
    vector_tile_uri,
)
from gratisgis_qgis.offline.clone import CLONE_SOURCE_FIELDS, CLONE_SOURCE_TABLE
from tests.plugin.conftest import install_qgis_stub

_PORTAL = "https://portal.example"

_CLONE_WIDGETS = [
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QFileDialog",
    "QFormLayout",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QMessageBox",
    "QProgressBar",
    "QPushButton",
    "QVBoxLayout",
    "QWidget",
]

_PUSH_WIDGETS = [
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QFormLayout",
    "QLabel",
    "QListWidget",
    "QListWidgetItem",
    "QMessageBox",
    "QVBoxLayout",
    "QWidget",
]


def _widget_stubs(names: list[str]) -> dict[str, object]:
    return {name: type(name, (), {}) for name in names}


@pytest.fixture
def clone_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsCoordinateReferenceSystem": type("QgsCRS", (), {}),
                "QgsProject": type("QgsProject", (), {}),
                "QgsVectorLayer": type("QgsVectorLayer", (), {}),
            },
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": _widget_stubs(_CLONE_WIDGETS),
        },
    )
    import gratisgis_qgis.ui.clone_dialog as mod

    return mod


@pytest.fixture
def push_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsFeature": type("QgsFeature", (), {}),
                "QgsProject": type("QgsProject", (), {}),
                "QgsVectorLayer": type("QgsVectorLayer", (), {}),
            },
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": _widget_stubs(_PUSH_WIDGETS),
        },
    )
    import gratisgis_qgis.ui.push_edits_dialog as mod

    return mod


# ----- Stand-ins for the Qt / QGIS objects the pickers touch -----


class _FakeVectorLayer:
    def __init__(self, name: str, source: str) -> None:
        self._name = name
        self._source = source

    def name(self) -> str:  # QGIS API name
        return self._name

    def source(self) -> str:  # QGIS API name
        return self._source

    def editBuffer(self) -> None:  # QGIS API name
        # Not in edit mode: the picker only cares that the layer is
        # offered at all, and a 0-op plan is a valid outcome.
        return None


class _NotAVectorLayer:
    def name(self) -> str:
        return "raster"

    def source(self) -> str:
        return oapif_uri(_PORTAL, "item-1__roads")


class _FakeCombo:
    def __init__(self) -> None:
        self.items: list[tuple[str, Any]] = []
        self.enabled = True

    def addItem(self, text: str, userData: Any = None) -> None:  # QGIS API name
        self.items.append((text, userData))

    def count(self) -> int:  # Qt API name
        return len(self.items)

    def setEnabled(self, value: bool) -> None:  # Qt API name
        self.enabled = value

    def isEnabled(self) -> bool:  # Qt API name
        return self.enabled

    @property
    def labels(self) -> list[str]:
        return [text for text, _ in self.items]


class _FakeLabel:
    def __init__(self) -> None:
        self.value = ""

    def setText(self, text: str) -> None:  # Qt API name
        self.value = text


class _FakeListWidget:
    def clear(self) -> None:  # Qt API name
        return None


def _fake_project(layers: dict[str, Any]) -> Any:
    class _Project:
        @staticmethod
        def instance() -> Any:
            return SimpleNamespace(mapLayers=lambda: layers)

    return _Project


def _populate(
    mod: ModuleType,
    dialog_name: str,
    monkeypatch: pytest.MonkeyPatch,
    layers: dict[str, Any],
) -> _FakeCombo:
    """Run the dialog's real combo-population method over ``layers``."""
    monkeypatch.setattr(mod, "QgsVectorLayer", _FakeVectorLayer)
    monkeypatch.setattr(mod, "QgsProject", _fake_project(layers))
    combo = _FakeCombo()
    getattr(mod, dialog_name)._populate_layer_combo(
        SimpleNamespace(_layer_combo=combo)
    )
    return combo


def _write_clone_gpkg(path: Path, *, item_id: str, layer_id: str) -> str:
    """Create a GeoPackage-shaped file carrying a clone-origin row.

    A GeoPackage is a SQLite database, and the origin table is a plain
    attribute table, so sqlite3 produces exactly what the writer does
    for the columns this reads. Both sides key off the same constants,
    so a rename cannot leave this test passing against a stale name.
    """
    conn = sqlite3.connect(str(path))
    try:
        columns = ", ".join(f'"{name}" TEXT' for name in CLONE_SOURCE_FIELDS)
        placeholders = ", ".join("?" for _ in CLONE_SOURCE_FIELDS)
        conn.execute(f'CREATE TABLE "{CLONE_SOURCE_TABLE}" ({columns})')
        conn.execute(
            f'INSERT INTO "{CLONE_SOURCE_TABLE}" VALUES ({placeholders})',
            (_PORTAL, item_id, layer_id, "2026-08-14T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return f"{path}|layername=clone"


class TestCloneDialogLayerList:
    """Cloning needs only an item id and a layer id, so every shape
    the tree emits qualifies.
    """

    def test_lists_a_public_vector_tile_layer(
        self, clone_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact reported bug: a spatial sublayer of a public item.
        layer = _FakeVectorLayer(
            "Trails", vector_tile_uri(_PORTAL, "item-1__trails")
        )
        combo = _populate(
            clone_mod, "CloneToGeoPackageDialog", monkeypatch, {"L1": layer}
        )
        assert combo.items == [("Trails", "L1")]
        assert combo.enabled

    def test_lists_an_authed_vector_tile_layer(
        self, clone_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same bug for private and org items, which is most of them.
        layer = _FakeVectorLayer(
            "Parcels",
            authed_vector_tile_uri(
                _PORTAL, "item-9", "parcels", authcfg_id="lyr1234"
            ),
        )
        combo = _populate(
            clone_mod, "CloneToGeoPackageDialog", monkeypatch, {"L2": layer}
        )
        assert combo.items == [("Parcels", "L2")]

    def test_lists_an_oapif_layer(
        self, clone_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layer = _FakeVectorLayer("Lookup", oapif_uri(_PORTAL, "item-1__lookup"))
        combo = _populate(
            clone_mod, "CloneToGeoPackageDialog", monkeypatch, {"L3": layer}
        )
        assert combo.items == [("Lookup", "L3")]

    def test_skips_off_portal_layers_and_says_so(
        self, clone_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layer = _FakeVectorLayer("Local", "/home/matt/roads.shp")
        combo = _populate(
            clone_mod, "CloneToGeoPackageDialog", monkeypatch, {"L4": layer}
        )
        assert combo.items == [("(no portal-backed layers in project)", None)]
        assert not combo.enabled

    def test_skips_non_vector_layers(
        self, clone_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        combo = _populate(
            clone_mod,
            "CloneToGeoPackageDialog",
            monkeypatch,
            {"R1": _NotAVectorLayer()},
        )
        assert combo.items == [("(no portal-backed layers in project)", None)]


class TestPushDialogLayerList:
    """Pushing needs an EDITABLE layer, which vector tiles are not."""

    @pytest.mark.parametrize(
        "source",
        [
            vector_tile_uri(_PORTAL, "item-1__trails"),
            authed_vector_tile_uri(_PORTAL, "item-1", "trails", authcfg_id="a"),
        ],
    )
    def test_never_lists_a_vector_tile_layer(
        self, push_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, source: str
    ) -> None:
        # QGIS vector tiles are a read-only rendering format. Offering
        # one here could only produce an empty plan and a confused user.
        layer = _FakeVectorLayer("Trails", source)
        combo = _populate(push_mod, "PushEditsDialog", monkeypatch, {"L1": layer})
        assert combo.items == [("(no editable portal layers in project)", None)]
        assert not combo.enabled

    def test_lists_an_oapif_layer(
        self, push_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layer = _FakeVectorLayer("Lookup", oapif_uri(_PORTAL, "item-1__lookup"))
        combo = _populate(push_mod, "PushEditsDialog", monkeypatch, {"L2": layer})
        assert combo.items == [("Lookup", "L2")]

    def test_lists_an_offline_clone(
        self,
        push_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Closing the round trip: clone, edit offline, push back.
        source = _write_clone_gpkg(
            tmp_path / "trails.gpkg", item_id="item-1", layer_id="trails"
        )
        layer = _FakeVectorLayer("Trails (offline)", source)
        combo = _populate(push_mod, "PushEditsDialog", monkeypatch, {"L3": layer})
        assert combo.items == [("Trails (offline)", "L3")]

    def test_skips_an_unrelated_geopackage(
        self,
        push_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Most GeoPackages in a project have nothing to do with the
        # portal; probing one must be quiet and negative.
        plain = tmp_path / "survey.gpkg"
        sqlite3.connect(str(plain)).close()
        layer = _FakeVectorLayer("Survey", f"{plain}|layername=survey")
        combo = _populate(push_mod, "PushEditsDialog", monkeypatch, {"L4": layer})
        assert combo.items == [("(no editable portal layers in project)", None)]


class TestPushDialogTargetResolution:
    """The ids the dialog resolves are the ids the push is sent to."""

    def _changed(
        self, push_mod: ModuleType, layer: Any, *, combo: _FakeCombo
    ) -> SimpleNamespace:
        state = SimpleNamespace(
            _plan=None,
            _ops_list=_FakeListWidget(),
            _skipped_list=_FakeListWidget(),
            _summary_label=_FakeLabel(),
            _layer_combo=combo,
            _target_item_id=None,
            _target_layer_id=None,
            _selected_layer=lambda: layer,
            _render_plan=lambda plan: None,
        )
        push_mod.PushEditsDialog._on_layer_changed(state)
        return state

    def test_oapif_layer_resolves_item_and_layer(
        self, push_mod: ModuleType
    ) -> None:
        layer = _FakeVectorLayer("Lookup", oapif_uri(_PORTAL, "item-1__lookup"))
        state = self._changed(push_mod, layer, combo=_FakeCombo())
        assert state._target_item_id == "item-1"
        assert state._target_layer_id == "lookup"

    def test_bare_collection_id_resolves_to_default_layer(
        self, push_mod: ModuleType
    ) -> None:
        layer = _FakeVectorLayer("Old", oapif_uri(_PORTAL, "item-1"))
        state = self._changed(push_mod, layer, combo=_FakeCombo())
        assert state._target_item_id == "item-1"
        assert state._target_layer_id == "default"

    def test_offline_clone_resolves_to_its_recorded_origin(
        self, push_mod: ModuleType, tmp_path: Path
    ) -> None:
        source = _write_clone_gpkg(
            tmp_path / "trails.gpkg", item_id="item-7", layer_id="trails"
        )
        state = self._changed(
            push_mod, _FakeVectorLayer("Trails (offline)", source), combo=_FakeCombo()
        )
        assert state._target_item_id == "item-7"
        assert state._target_layer_id == "trails"

    def test_empty_project_explains_the_read_only_tree_layers(
        self, push_mod: ModuleType
    ) -> None:
        # The empty state has to name the fix, because a user looking
        # at a portal layer in their Layers panel cannot otherwise tell
        # why this dialog says there is nothing to push.
        combo = _FakeCombo()
        combo.addItem("(no editable portal layers in project)", None)
        combo.setEnabled(False)
        state = self._changed(push_mod, None, combo=combo)
        assert "read" in state._summary_label.value.lower()
        assert "Clone layer for offline use" in state._summary_label.value
