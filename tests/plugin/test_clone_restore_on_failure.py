# SPDX-License-Identifier: AGPL-3.0-or-later
"""The layer has to come back when the overwrite fails.

Reported from a real session: cloning the same layer a second time
removed it from the Layers panel and never added it back. The write
failed after the removal, and the failure paths returned straight to an
error box.

That is the worst shape a failure can take here. The file on disk was
untouched, so nothing was gained, and the layer and its styling were
gone from the project, so something was lost. A refusal that changes
nothing is a fine outcome; a refusal that costs the user their layer is
not.

The removal cannot be avoided, since the file has to be released before
it can be written. So every path out of that write owes the layer back.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any, ClassVar

import pytest

from tests.plugin.conftest import install_qgis_stub

_WIDGETS = [
    "QApplication", "QCheckBox", "QComboBox", "QDialog", "QDialogButtonBox",
    "QFileDialog", "QFormLayout", "QHBoxLayout", "QLabel", "QLineEdit",
    "QListWidget", "QListWidgetItem", "QMessageBox", "QPlainTextEdit",
    "QProgressBar", "QPushButton", "QTextEdit", "QVBoxLayout", "QWidget",
]


class _Project:
    singleton: _Project | None = None

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.removed: list[str] = []

    @classmethod
    def instance(cls) -> _Project:
        assert cls.singleton is not None
        return cls.singleton

    def addMapLayer(self, layer: Any) -> Any:  # QGIS API name
        self.added.append(layer)
        return layer

    def removeMapLayer(self, layer_id: str) -> None:  # QGIS API name
        self.removed.append(layer_id)

    def mapLayers(self) -> dict[str, Any]:  # QGIS API name
        return {}


class _Layer:
    """A QgsVectorLayer built from a URI, valid unless told otherwise."""

    valid = True
    built: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, uri: str, name: str, _provider: str) -> None:
        self.uri = uri
        self._name = name
        type(self).built.append((uri, name))

    def isValid(self) -> bool:  # QGIS API name
        return type(self).valid

    def id(self) -> str:  # QGIS API name
        return "restored"

    def name(self) -> str:  # QGIS API name
        return self._name


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _Project.singleton = _Project()
    _Layer.valid = True
    _Layer.built = []
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsProject": _Project,
                "QgsVectorLayer": _Layer,
                "QgsVectorTileLayer": type("QgsVectorTileLayer", (), {}),
                "QgsCoordinateReferenceSystem": type("C", (), {}),
            },
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QSettings": type("QSettings", (), {}),
                "QVariant": type("QVariant", (), {}),
            },
            "qgis.PyQt.QtWidgets": {n: type(n, (), {}) for n in _WIDGETS},
        },
    )
    import gratisgis_qgis.ui.clone_dialog as m

    return m


def _dialog(mod: ModuleType) -> Any:
    dlg = mod.CloneToGeoPackageDialog.__new__(mod.CloneToGeoPackageDialog)
    return dlg


def _target(mod: ModuleType) -> Any:
    # gpkg_path is derived, not stored; the real dataclass takes the
    # directory and the stem.
    return mod.CloneTarget(directory="C:/data", file_name="clone")


class TestRestoreRemoved:
    def test_the_layer_is_added_back_from_the_untouched_file(
        self, mod: ModuleType
    ) -> None:
        """safe_write_path never promoted, so the file is as it was."""
        dlg = _dialog(mod)
        ok = dlg._restore_removed(
            _target(mod), ["Parcels (offline)"], mod.LayerPlacement()
        )
        assert ok is True
        assert _Project.instance().added, "nothing was put back"
        uri, name = _Layer.built[-1]
        # Separator comes from os.path.join, so compare on the parts.
        assert uri == f"{_target(mod).gpkg_path}|layername=clone"
        assert uri.endswith("clone.gpkg|layername=clone")
        assert name == "Parcels (offline)", "it keeps the name it had"

    def test_the_styling_comes_back_with_it(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Losing the layer is bad; getting it back unstyled is still a loss."""
        applied: list[Any] = []
        monkeypatch.setattr(
            mod, "restore_placement", lambda _l, p: applied.append(p)
        )
        placement = mod.LayerPlacement(style_xml="<qgis/>", index=2)
        dlg = _dialog(mod)
        dlg._restore_removed(_target(mod), ["Parcels"], placement)
        assert applied == [placement]

    def test_nothing_removed_means_nothing_to_restore(
        self, mod: ModuleType
    ) -> None:
        dlg = _dialog(mod)
        assert dlg._restore_removed(_target(mod), [], mod.LayerPlacement())
        assert _Project.instance().added == []

    def test_a_file_that_will_not_reopen_is_reported_not_raised(
        self, mod: ModuleType
    ) -> None:
        """The user is already being shown one error; do not add a crash.

        Returning False lets the message say the layer could not be
        reopened, which is the honest outcome and tells them to add it
        back from the Browser panel.
        """
        _Layer.valid = False
        dlg = _dialog(mod)
        assert dlg._restore_removed(
            _target(mod), ["Parcels"], mod.LayerPlacement()
        ) is False
        assert _Project.instance().added == []


class TestTheMessage:
    def test_a_restored_layer_is_accounted_for(self, mod: ModuleType) -> None:
        """The panel flickers, so the error box has to explain it.

        Silence here reads as "the plugin ate my layer", which is what
        it used to do.
        """
        note = mod._restored_note(["Parcels (offline)"], True)
        assert "Parcels (offline)" in note
        assert "unchanged" in note

    def test_a_layer_that_could_not_be_restored_says_what_to_do(
        self, mod: ModuleType
    ) -> None:
        note = mod._restored_note(["Parcels (offline)"], False)
        assert "Parcels (offline)" in note
        assert "Browser" in note
        assert "unchanged" in note, "the file itself is still fine; say so"

    def test_no_removal_adds_nothing_to_the_message(
        self, mod: ModuleType
    ) -> None:
        """A first clone removes nothing, so there is nothing to explain."""
        assert mod._restored_note([], True) == ""

    def test_the_message_never_blames_another_program_outright(
        self, mod: ModuleType
    ) -> None:
        """The old wording sent users hunting for a program to close.

        The usual holder is QGIS itself, through a pooled dataset, so
        "close it in another program" was advice that could not work.
        """
        note = mod._restored_note(["Parcels"], True)
        assert "another program" not in note
