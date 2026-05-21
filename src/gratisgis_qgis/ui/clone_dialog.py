# SPDX-License-Identifier: AGPL-3.0-or-later
"""Clone-to-GeoPackage dialog (Phase 7).

Pulls a full feature set for a portal-backed layer and writes it
to a local GeoPackage so the user can keep working offline.

Flow:

  1. List vector layers in the project whose source URI is an
     OAPIF endpoint we recognize.
  2. User picks one + a target directory.
  3. Plugin downloads the full GeoJSON FeatureCollection via the
     client's `features.download_geojson(...)`.
  4. Plugin normalizes the response (move portal ids into
     _portal_id property) and writes the result to a GeoPackage.
  5. Plugin loads the GeoPackage as a new project layer so the
     user immediately sees the clone.

The pure-Python pieces (target sanitization, feature
normalization, validation) live in `offline/clone.py` and are
tested without QGIS.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import TYPE_CHECKING

from qgis.core import (  # type: ignore[import-not-found]
    QgsCoordinateReferenceSystem,
    QgsMapLayer,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..browser.fetch import _connected_client, _run
from ..browser.uris import parse_oapif_uri
from ..log import get_logger
from ..offline.clone import (
    CloneTarget,
    make_target,
    normalize_feature_collection,
    validate_clone_target,
)
from ..settings import ConnectionStore

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)


class CloneToGeoPackageDialog(QDialog):
    """Modal dialog driving the offline-clone flow."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clone GratisGIS layer for offline use")
        self.setMinimumWidth(560)
        self._iface = iface
        self._store = ConnectionStore()
        self._target_directory: str = ""

        self._layer_combo = QComboBox()
        self._populate_layer_combo()
        self._connection_combo = QComboBox()
        self._populate_connection_combo()

        self._directory_label = QLabel("(no directory chosen)")
        choose_button = QPushButton("Choose directory...")
        choose_button.clicked.connect(self._on_pick_directory)

        self._filename_input = QLineEdit()

        form = QFormLayout()
        form.addRow("Layer:", self._layer_combo)
        form.addRow("Portal:", self._connection_combo)
        form.addRow("Destination:", self._directory_label)
        form.addRow("", choose_button)
        form.addRow("File name:", self._filename_input)

        self._issues_list = QListWidget()
        self._issues_list.setFixedHeight(90)

        self._progress_label = QLabel("")
        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Clone")
        buttons.accepted.connect(self._on_clone)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(QLabel("Pre-flight checks:"))
        root.addWidget(self._issues_list)
        root.addWidget(self._progress_label)
        root.addWidget(self._progress_bar)
        root.addWidget(buttons)
        self.setLayout(root)

        self._layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        self._on_layer_changed()

    def _populate_layer_combo(self) -> None:
        project = QgsProject.instance()
        for layer_id, layer in project.mapLayers().items():
            if layer.type() != QgsMapLayer.VectorLayer:
                continue
            assert isinstance(layer, QgsVectorLayer)
            if parse_oapif_uri(layer.source()) is None:
                continue
            self._layer_combo.addItem(layer.name(), userData=layer_id)
        if self._layer_combo.count() == 0:
            self._layer_combo.addItem("(no portal-backed layers in project)", None)
            self._layer_combo.setEnabled(False)

    def _populate_connection_combo(self) -> None:
        for name in self._store.list_names():
            profile = self._store.get(name)
            if profile is None or not profile.is_discovered:
                continue
            self._connection_combo.addItem(profile.display_label, userData=name)
        if self._connection_combo.count() == 0:
            self._connection_combo.addItem("(no signed-in connections)", None)
            self._connection_combo.setEnabled(False)

    def _on_pick_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output directory")
        if not path:
            return
        self._target_directory = path
        self._directory_label.setText(path)
        self._refresh_validation()

    def _on_layer_changed(self) -> None:
        layer = self._selected_layer()
        if layer is not None:
            parsed = parse_oapif_uri(layer.source())
            if parsed:
                _portal, type_name = parsed
                layer_id = type_name.split("__", 1)[1] if "__" in type_name else "default"
                target = make_target(
                    directory=self._target_directory or tempfile.gettempdir(),
                    item_title=layer.name(),
                    layer_id=layer_id,
                )
                self._filename_input.setText(target.file_name)
        self._refresh_validation()

    def _refresh_validation(self) -> None:
        self._issues_list.clear()
        if not self._target_directory:
            self._issues_list.addItem(QListWidgetItem("Choose a destination directory."))
            return
        target = CloneTarget(
            directory=self._target_directory,
            file_name=self._filename_input.text().strip() or "clone",
        )
        issues = validate_clone_target(target)
        if not issues:
            row = QListWidgetItem("All checks passed.")
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
            self._issues_list.addItem(row)
            return
        for issue in issues:
            marker = "[ERROR]" if issue.is_error else "[warn]"
            row = QListWidgetItem(f"{marker} {issue.message}")
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
            self._issues_list.addItem(row)

    def _selected_layer(self) -> QgsVectorLayer | None:
        layer_id = self._layer_combo.currentData()
        if not layer_id:
            return None
        layer = QgsProject.instance().mapLayer(layer_id)
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _on_clone(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            QMessageBox.warning(self, "No layer", "Pick a portal-backed layer.")
            return
        profile_name = self._connection_combo.currentData()
        if not profile_name:
            QMessageBox.warning(self, "No connection", "Pick a signed-in connection.")
            return
        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            QMessageBox.warning(self, "Connection not ready", "Sign in first.")
            return

        parsed = parse_oapif_uri(layer.source())
        if parsed is None:
            QMessageBox.critical(
                self,
                "Unresolved layer",
                "Could not resolve the portal item from the layer source.",
            )
            return
        _portal_url, type_name = parsed
        if "__" in type_name:
            item_id, layer_id = type_name.split("__", 1)
        else:
            item_id, layer_id = type_name, "default"

        target = CloneTarget(
            directory=self._target_directory,
            file_name=self._filename_input.text().strip() or "clone",
        )
        issues = validate_clone_target(target)
        blocking = [i for i in issues if i.is_error]
        if blocking:
            QMessageBox.critical(
                self,
                "Validation failed",
                "\n".join(f"- {i.message}" for i in blocking),
            )
            return
        overwrite_warns = [i for i in issues if i.code == "target-exists"]
        if overwrite_warns:
            ok = QMessageBox.question(
                self,
                "Overwrite?",
                f"{overwrite_warns[0].message}\n\nProceed?",
            )
            if ok != QMessageBox.Yes:
                return

        self._buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._progress_label.setText("Downloading features from portal...")

        try:
            body = _run(_download(profile=profile, item_id=item_id, layer_id=layer_id))
        except Exception as e:
            _log.exception("download failed")
            QMessageBox.critical(self, "Download failed", str(e))
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return

        fc = normalize_feature_collection(body)
        count = len(fc.get("features", []))
        self._progress_label.setText(f"Writing {count} feature(s) to GeoPackage...")

        try:
            _write_geojson_to_geopackage(fc, target.gpkg_path)
        except Exception as e:
            _log.exception("geopackage write failed")
            QMessageBox.critical(self, "Write failed", str(e))
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return

        # Load the new GeoPackage into the project as a sibling
        # of the cloned-from layer so the user sees the result
        # immediately.
        local = QgsVectorLayer(
            f"{target.gpkg_path}|layername={target.file_name}",
            f"{layer.name()} (offline)",
            "ogr",
        )
        if local.isValid():
            QgsProject.instance().addMapLayer(local)
        else:
            _log.warning("clone produced an invalid layer at %s", target.gpkg_path)

        QMessageBox.information(
            self,
            "Cloned",
            f"{count} feature(s) written to:\n{target.gpkg_path}",
        )
        self.accept()


# -----------------------------------------------------------
# Async + QGIS bridges
# -----------------------------------------------------------


async def _download(*, profile, item_id: str, layer_id: str):
    async with _connected_client(profile) as client:
        return await client.features.download_geojson(
            item_id=item_id, layer_id=layer_id
        )


def _write_geojson_to_geopackage(
    feature_collection: dict, gpkg_path: str
) -> None:
    """Write the normalized FeatureCollection to a GeoPackage.

    Uses QGIS's OGR provider: load the FeatureCollection as a
    QgsVectorLayer (the "ogr" provider accepts a memory:// path
    when fed an in-memory GeoJSON file) and write it out with
    QgsVectorFileWriter. Round-tripping via a tempfile keeps the
    helper independent of the QgsVectorFileWriter version
    (writeAsVectorFormatV3 vs the legacy API).
    """
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter

    # Tempfile to hand to OGR.
    fd, tmp_geojson = tempfile.mkstemp(suffix=".geojson", prefix="gratisgis-clone-")
    os.close(fd)
    try:
        with open(tmp_geojson, "w", encoding="utf-8") as fh:
            json.dump(feature_collection, fh)

        loader = QgsVectorLayer(tmp_geojson, "clone-src", "ogr")
        if not loader.isValid():
            raise RuntimeError(
                "GeoJSON staging file failed to load via OGR; check the "
                "feature collection shape."
            )
        # GeoJSON is always CRS84; pin so the GeoPackage records EPSG:4326.
        if not loader.crs().isValid():
            loader.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))

        if os.path.exists(gpkg_path):
            os.unlink(gpkg_path)

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.fileEncoding = "UTF-8"

        ctx = QgsCoordinateTransformContext()
        err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
            loader, gpkg_path, ctx, options
        )
        if err != QgsVectorFileWriter.NoError:
            raise RuntimeError(f"GeoPackage write failed: {msg or err}")
    finally:
        try:
            os.unlink(tmp_geojson)
        except OSError:
            pass
