# SPDX-License-Identifier: AGPL-3.0-or-later
"""Clone-to-GeoPackage dialog (Phase 7).

Pulls a full feature set for a portal-backed layer and writes it
to a local GeoPackage so the user can keep working offline.

Flow:

  1. List vector layers in the project whose source URI resolves
     to a portal item, in any of the shapes the Browser tree emits
     (OAPIF for tables, public or authed vector tiles for spatial
     sublayers).
  2. User picks one + a target directory.
  3. Plugin downloads the full GeoJSON FeatureCollection via the
     client's `features.download_geojson(...)`.
  4. Plugin normalizes the response (move portal ids into
     _portal_id property) and writes the result to a GeoPackage,
     alongside a small table recording which portal layer it came
     from so the push-edits flow can send edits back.
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

from ..browser.uris import PortalLayerRef, parse_portal_layer_source
from ..log import get_logger
from ..offline.clone import (
    CLONE_SOURCE_FIELDS,
    CLONE_SOURCE_TABLE,
    CloneTarget,
    clone_timestamp,
    make_target,
    normalize_feature_collection,
    safe_write_path,
    validate_clone_target,
)
from ..portal import get_client
from ..qgis_compat import resolve_enum
from ..settings import ConnectionStore
from ..tasks import format_error, run_in_task

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
        # Set on reject: a download completing after the user
        # dismissed the dialog must not write files or add layers.
        self._closed = False

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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Clone")
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
        # isinstance covers both QGIS 3 and QGIS 4; the
        # QgsMapLayer.VectorLayer integer constant was retired in
        # QGIS 4 in favor of Qgis.LayerType.Vector.
        project = QgsProject.instance()
        for layer_id, layer in project.mapLayers().items():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if parse_portal_layer_source(layer.source()) is None:
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
            ref = parse_portal_layer_source(layer.source())
            if ref is not None:
                target = make_target(
                    directory=self._target_directory or tempfile.gettempdir(),
                    item_title=layer.name(),
                    layer_id=ref.layer_id,
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
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._issues_list.addItem(row)
            return
        for issue in issues:
            marker = "[ERROR]" if issue.is_error else "[warn]"
            row = QListWidgetItem(f"{marker} {issue.message}")
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
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

        ref = parse_portal_layer_source(layer.source())
        if ref is None:
            QMessageBox.critical(
                self,
                "Unresolved layer",
                "Could not resolve the portal item from the layer source.",
            )
            return
        item_id, layer_id = ref.item_id, ref.layer_id

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
            if ok != QMessageBox.StandardButton.Yes:
                return

        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._progress_label.setText("Downloading features from portal...")
        layer_name = layer.name()

        def download(_handle):
            return get_client(profile).features.download_geojson(
                item_id=item_id, layer_id=layer_id
            )

        def done(body) -> None:
            if self._closed:
                return
            self._write_and_load(
                body, target=target, layer_name=layer_name, source=ref
            )

        def failed(exc: BaseException) -> None:
            _log.error("download failed", exc_info=exc)
            if self._closed:
                return
            QMessageBox.critical(self, "Download failed", format_error(exc))
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)

        run_in_task("GratisGIS: clone layer", download, done, failed, cancelable=False)

    def reject(self) -> None:  # Qt override
        self._closed = True
        super().reject()

    def _write_and_load(
        self,
        body,
        *,
        target: CloneTarget,
        layer_name: str,
        source: PortalLayerRef,
    ) -> None:
        # The GeoPackage write and layer registration use QGIS API
        # objects, so they stay on the GUI thread; only the network
        # download runs in the task.
        fc = normalize_feature_collection(body)
        count = len(fc.get("features", []))
        self._progress_label.setText(f"Writing {count} feature(s) to GeoPackage...")

        try:
            _write_geojson_to_geopackage(fc, target.gpkg_path, source=source)
        except Exception as e:
            _log.exception("geopackage write failed")
            QMessageBox.critical(self, "Write failed", str(e))
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return

        # Load the new GeoPackage into the project as a sibling
        # of the cloned-from layer so the user sees the result
        # immediately.
        local = QgsVectorLayer(
            f"{target.gpkg_path}|layername={target.file_name}",
            f"{layer_name} (offline)",
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
# QGIS bridges
# -----------------------------------------------------------


def _write_geojson_to_geopackage(
    feature_collection: dict, gpkg_path: str, *, source: PortalLayerRef | None = None
) -> None:
    """Write the normalized FeatureCollection to a GeoPackage.

    Uses QGIS's OGR provider: load the FeatureCollection as a
    QgsVectorLayer (the "ogr" provider accepts a memory:// path
    when fed an in-memory GeoJSON file) and write it out with
    QgsVectorFileWriter. Round-tripping via a tempfile keeps the
    helper independent of the QgsVectorFileWriter version
    (writeAsVectorFormatV3 vs the legacy API).

    ``source`` is stamped into the same container as a second,
    non-spatial table so the push-edits flow can offer the clone
    back to its origin layer; see ``offline.clone``.

    The GeoPackage itself is written via ``safe_write_path``: the
    writer targets a sibling temp file that only replaces
    ``gpkg_path`` once the write succeeded, so a failed re-clone
    can never destroy an existing (possibly locally edited) copy.
    Both tables are written inside that one block, so a clone that
    fails halfway still cannot replace a good file with a partial
    one.
    """
    import contextlib

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

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.fileEncoding = "UTF-8"
        # The GPKG's internal layer name used to default to the output
        # file's stem back when the final path was written directly.
        # The safe-write temp path carries a random name, so the stem
        # has to be pinned explicitly or the "|layername=" reference
        # the loader builds afterwards would not resolve.
        options.layerName = os.path.splitext(os.path.basename(gpkg_path))[0]

        # QGIS 3 exposed NoError as a class-level shortcut; the scoped
        # WriterError enum is its home on newer builds and the only
        # spelling under QGIS 4's strict PyQt6.
        no_error = resolve_enum(
            (getattr(QgsVectorFileWriter, "WriterError", None), "NoError"),
            (QgsVectorFileWriter, "NoError"),
        )
        ctx = QgsCoordinateTransformContext()
        with safe_write_path(gpkg_path) as tmp_gpkg:
            err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
                loader, tmp_gpkg, ctx, options
            )
            if err != no_error:
                raise RuntimeError(f"GeoPackage write failed: {msg or err}")
            if source is not None:
                # Best-effort by design: the features are already
                # written, and losing the origin stamp only costs the
                # user the push-back shortcut. Failing the clone here
                # would throw away a download that may have taken
                # minutes.
                try:
                    _write_clone_source_table(tmp_gpkg, source, ctx=ctx)
                except Exception:
                    _log.exception("clone-source table could not be written")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_geojson)


def _write_clone_source_table(
    gpkg_path: str, source: PortalLayerRef, *, ctx
) -> None:
    """Add the clone-origin table to an already-written GeoPackage.

    ``CreateOrOverwriteLayer`` is the load-bearing detail: the
    default action for an existing file is CreateOrOverwriteFile,
    which would drop the feature layer written a moment ago and
    leave a GeoPackage holding nothing but provenance.

    Called inside the caller's ``safe_write_path`` block against the
    temp path, so the origin table lands in the same container as
    the data and the same atomic promote covers both.
    """
    from qgis.core import QgsFeature, QgsVectorFileWriter

    field_spec = "&".join(f"field={name}:string" for name in CLONE_SOURCE_FIELDS)
    # "None" geometry: the origin row has no location, and a
    # geometry column would make QGIS list it as a map layer.
    holder = QgsVectorLayer(f"None?{field_spec}", CLONE_SOURCE_TABLE, "memory")
    if not holder.isValid():
        _log.warning("clone-source table skipped: memory layer did not build")
        return
    values = {
        "portal_url": source.portal_url,
        "item_id": source.item_id,
        "layer_id": source.layer_id,
        "cloned_at": clone_timestamp(),
    }
    feature = QgsFeature(holder.fields())
    feature.setAttributes([values[name] for name in CLONE_SOURCE_FIELDS])
    added, _ = holder.dataProvider().addFeatures([feature])
    if not added:
        _log.warning("clone-source table skipped: origin row was rejected")
        return

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.fileEncoding = "UTF-8"
    options.layerName = CLONE_SOURCE_TABLE
    options.actionOnExistingFile = resolve_enum(
        (
            getattr(QgsVectorFileWriter, "ActionOnExistingFile", None),
            "CreateOrOverwriteLayer",
        ),
        (QgsVectorFileWriter, "CreateOrOverwriteLayer"),
    )
    no_error = resolve_enum(
        (getattr(QgsVectorFileWriter, "WriterError", None), "NoError"),
        (QgsVectorFileWriter, "NoError"),
    )
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        holder, gpkg_path, ctx, options
    )
    if err != no_error:
        _log.warning("clone-source table skipped: %s", msg or err)
