# SPDX-License-Identifier: AGPL-3.0-or-later
"""Created-feature id capture in the push-edits flow.

The defect: pushed creates used to drop the portal-assigned id, so
pushing again before a commit re-created every one of them
server-side. Pinned here: the create op returns the id from the
append response, the write-back stamps it into the clone flow's
portal-id column when present (and only logs when absent), and an
added feature that already carries a portal id re-pushes as an
update, not a second create.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from gratisgis_client.endpoints.features import AppendResult
from gratisgis_qgis.offline.clone import PORTAL_ID_PROPERTY
from tests.plugin.conftest import install_qgis_stub

_WIDGET_NAMES = [
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


@pytest.fixture
def dialog_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
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
            "qgis.PyQt.QtWidgets": {
                name: type(name, (), {}) for name in _WIDGET_NAMES
            },
        },
    )
    import gratisgis_qgis.ui.push_edits_dialog as mod

    return mod


class TestApplyOpReturnsCreatedId:
    def _client(self, result: AppendResult) -> SimpleNamespace:
        calls: list[dict[str, Any]] = []

        def append(**kwargs: Any) -> AppendResult:
            calls.append(kwargs)
            return result

        return SimpleNamespace(
            features=SimpleNamespace(append=append), _calls=calls
        )

    def test_create_returns_first_global_id(self, dialog_mod: ModuleType) -> None:
        from gratisgis_qgis.edit.sync import SyncOp

        client = self._client(AppendResult(inserted=1, global_ids=["gid-1"]))
        op = SyncOp(kind="create", qgis_fid=-2, portal_id=None, properties={"k": 1})
        new_id = dialog_mod._apply_op(client, item_id="i", layer_id="l", op=op)
        assert new_id == "gid-1"

    def test_create_without_ids_returns_none(self, dialog_mod: ModuleType) -> None:
        from gratisgis_qgis.edit.sync import SyncOp

        client = self._client(AppendResult(inserted=1))
        op = SyncOp(kind="create", qgis_fid=-2, portal_id=None, properties={"k": 1})
        assert dialog_mod._apply_op(client, item_id="i", layer_id="l", op=op) is None


class _FakeField:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:  # QGIS API name
        return self._name


class _FakeFields:
    def __init__(self, names: list[str]) -> None:
        self._fields = [_FakeField(n) for n in names]

    def indexOf(self, name: str) -> int:  # QGIS API name
        for i, f in enumerate(self._fields):
            if f.name() == name:
                return i
        return -1

    def count(self) -> int:  # QGIS API name
        return len(self._fields)

    def __iter__(self) -> Any:
        return iter(self._fields)

    def __getitem__(self, i: int) -> _FakeField:
        return self._fields[i]


class _FakeLayer:
    def __init__(self, field_names: list[str]) -> None:
        self._fields = _FakeFields(field_names)
        self.changed: list[tuple[int, int, str]] = []

    def fields(self) -> _FakeFields:  # QGIS API name
        return self._fields

    def changeAttributeValue(self, fid: int, idx: int, value: str) -> bool:  # QGIS API name
        self.changed.append((fid, idx, value))
        return True


class TestWriteBackCreatedIds:
    def test_writes_into_portal_id_column(self, dialog_mod: ModuleType) -> None:
        layer = _FakeLayer(["name", PORTAL_ID_PROPERTY])
        dialog_mod._write_back_created_ids(layer, [(-2, "gid-1"), (-3, "gid-2")])
        assert layer.changed == [(-2, 1, "gid-1"), (-3, 1, "gid-2")]

    def test_absent_column_logs_and_skips(self, dialog_mod: ModuleType) -> None:
        # The common live-OAPIF case: the layer's schema is the
        # portal layer's own fields, no local bookkeeping column.
        # Best-effort means no writes, no exception.
        layer = _FakeLayer(["name"])
        dialog_mod._write_back_created_ids(layer, [(-2, "gid-1")])
        assert layer.changed == []

    def test_no_created_ids_is_a_noop(self, dialog_mod: ModuleType) -> None:
        layer = _FakeLayer([PORTAL_ID_PROPERTY])
        dialog_mod._write_back_created_ids(layer, [])
        assert layer.changed == []

    def test_missing_layer_is_a_noop(self, dialog_mod: ModuleType) -> None:
        dialog_mod._write_back_created_ids(None, [(-2, "gid-1")])


class _FakeFeat:
    """Duck-typed QgsFeature: mapping access + fields/attributes."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values
        self._names = list(values.keys())

    def __getitem__(self, key: str) -> Any:
        if key not in self._values:
            raise KeyError(key)
        return self._values[key]

    def hasGeometry(self) -> bool:  # QGIS API name
        return False

    def fields(self) -> _FakeFields:  # QGIS API name
        return _FakeFields(self._names)

    def attribute(self, i: int) -> Any:  # QGIS API name
        return self._values[self._names[i]]


class _FakeBuffer:
    def __init__(self, added: dict[int, _FakeFeat]) -> None:
        self._added = added

    def addedFeatures(self) -> dict[int, _FakeFeat]:  # QGIS API name
        return self._added

    def changedGeometries(self) -> dict[int, Any]:  # QGIS API name
        return {}

    def changedAttributeValues(self) -> dict[int, Any]:  # QGIS API name
        return {}

    def deletedFeatureIds(self) -> list[int]:  # QGIS API name
        return []


class _FakeEditLayer:
    def __init__(self, added: dict[int, _FakeFeat]) -> None:
        self._buffer = _FakeBuffer(added)

    def editBuffer(self) -> _FakeBuffer:  # QGIS API name
        return self._buffer

    def fields(self) -> _FakeFields:  # QGIS API name
        return _FakeFields([])


class TestCollectEditsRePush:
    def test_added_feature_with_written_back_id_becomes_update(
        self, dialog_mod: ModuleType
    ) -> None:
        # After a successful push + write-back the feature still sits
        # in the edit buffer as an add; the second push must send an
        # update addressed at the portal id, not a duplicate create.
        feat = _FakeFeat({PORTAL_ID_PROPERTY: "gid-1", "name": "x"})
        layer = _FakeEditLayer({-2: feat})
        [edit] = dialog_mod._collect_edits(layer)
        assert edit.kind == "update"
        assert edit.portal_id == "gid-1"
        # The bookkeeping column itself must not be pushed as data.
        assert edit.properties == {"name": "x"}

    def test_added_feature_without_id_stays_a_create(
        self, dialog_mod: ModuleType
    ) -> None:
        feat = _FakeFeat({"name": "x"})
        layer = _FakeEditLayer({-2: feat})
        [edit] = dialog_mod._collect_edits(layer)
        assert edit.kind == "create"
        assert edit.portal_id is None
        assert edit.properties == {"name": "x"}
