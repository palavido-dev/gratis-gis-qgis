# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish-vector-layer dialog (Phase 3).

The user picks a QGIS vector layer, the dialog runs pre-flight
validation, exports the layer to a GeoPackage tempfile, stages
the upload on the portal, creates a new ``data_layer`` item, and
enqueues an async import job. While the worker imports, the
dialog polls the job status and renders progress.

QGIS interaction lives here so the translation logic in
``publish/vector.py`` stays free of QGIS imports and testable
without QGIS.
"""
from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

from qgis.core import (  # type: ignore[import-not-found]
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import Qt, QTimer  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gratisgis_client.endpoints.import_jobs import ImportJob

from ..browser.fetch import _connected_client, _run
from ..log import get_logger
from ..publish.vector import (
    LayerSummary,
    build_data_layer_envelope,
    layer_from_probe,
    validate_layer,
)
from ..settings import ConnectionStore

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)

# How often to poll the import-jobs endpoint while a job is
# running. The portal worker flushes batch metrics every ~500 ms,
# so 1 s strikes a good balance between snappiness and load.
_POLL_INTERVAL_MS = 1000


class PublishVectorDialog(QDialog):
    """Modal dialog driving the vector-publish flow."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish vector layer to GratisGIS")
        self.setMinimumWidth(560)
        self._iface = iface
        self._store = ConnectionStore()
        self._poll_timer: QTimer | None = None
        self._current_job: ImportJob | None = None
        self._current_item_id: str | None = None
        self._current_layer_id: str | None = None
        self._current_profile_name: str | None = None

        # ----- Layer + connection picker -----
        self._layer_combo = QComboBox()
        self._populate_layer_combo()
        self._connection_combo = QComboBox()
        self._populate_connection_combo()

        self._title_input = QLineEdit()
        self._description_input = QPlainTextEdit()
        self._description_input.setFixedHeight(60)
        self._access_combo = QComboBox()
        self._access_combo.addItem("Private (only you)", "private")
        self._access_combo.addItem("Org (everyone in your org)", "org")
        self._access_combo.addItem("Public (anyone with the link)", "public")

        form = QFormLayout()
        form.addRow("Layer:", self._layer_combo)
        form.addRow("Portal:", self._connection_combo)
        form.addRow("Title:", self._title_input)
        form.addRow("Description:", self._description_input)
        form.addRow("Access:", self._access_combo)

        # ----- Validation surface -----
        self._issues_list = QListWidget()
        self._issues_list.setFixedHeight(110)

        # ----- Progress surface -----
        self._progress_label = QLabel("")
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setVisible(False)

        # ----- Buttons -----
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Publish")
        buttons.accepted.connect(self._on_publish)
        buttons.rejected.connect(self._on_cancel)
        self._buttons = buttons

        validate_button = QPushButton("Validate")
        validate_button.clicked.connect(self._on_validate)

        # ----- Compose layout -----
        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(QLabel("Pre-flight checks:"))
        root.addWidget(self._issues_list)
        root.addWidget(validate_button)
        root.addWidget(self._progress_label)
        root.addWidget(self._progress_bar)
        root.addWidget(buttons)
        self.setLayout(root)

        # Default-fill the title from the currently selected layer.
        self._layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        self._on_layer_changed()
        # Run an initial validation so the user sees the checks
        # without having to click.
        self._on_validate()

    # ----- Setup helpers -----

    def _populate_layer_combo(self) -> None:
        self._layer_combo.clear()
        project = QgsProject.instance()
        # isinstance check works on both QGIS 3 (where layer.type()
        # is QgsMapLayer.VectorLayer) and QGIS 4 (where the enum
        # moved to Qgis.LayerType.Vector). The Python identity check
        # is portable across both.
        for layer_id, layer in project.mapLayers().items():
            if not isinstance(layer, QgsVectorLayer):
                continue
            self._layer_combo.addItem(layer.name(), userData=layer_id)
        if self._layer_combo.count() == 0:
            self._layer_combo.addItem("(no vector layers in project)", None)
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

    def _on_layer_changed(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            self._title_input.setText("")
            return
        if not self._title_input.text().strip():
            self._title_input.setText(layer.name())

    # ----- Validation -----

    def _on_validate(self) -> None:
        self._issues_list.clear()
        layer = self._selected_layer()
        if layer is None:
            self._issues_list.addItem(
                QListWidgetItem("Select a vector layer first.")
            )
            return
        summary = _summary_from_layer(layer)
        issues = validate_layer(summary)
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
        if isinstance(layer, QgsVectorLayer):
            return layer
        return None

    # ----- Publish flow -----

    def _on_publish(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            QMessageBox.warning(self, "No layer selected", "Pick a vector layer to publish.")
            return

        profile_name = self._connection_combo.currentData()
        if not profile_name:
            QMessageBox.warning(
                self,
                "No connection selected",
                "Pick a signed-in connection to publish to.",
            )
            return

        summary = _summary_from_layer(layer)
        issues = validate_layer(summary)
        blocking = [i for i in issues if i.is_error]
        if blocking:
            QMessageBox.critical(
                self,
                "Validation failed",
                "Fix the following before publishing:\n\n"
                + "\n".join(f"- {i.message}" for i in blocking),
            )
            return

        title = self._title_input.text().strip() or layer.name()
        description = self._description_input.toPlainText().strip() or None
        access = self._access_combo.currentData() or "private"

        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            QMessageBox.warning(self, "Connection not ready", "Sign in to the connection first.")
            return

        # Lock the buttons + show "Exporting...". The portal upload
        # is the longest user-visible phase for big layers, but it
        # blocks our event loop. The dialog disables interaction so
        # the user can't fire a second publish during the upload.
        self._set_busy(True)
        self._progress_label.setText("Exporting layer to GeoPackage...")
        QTimer.singleShot(0, lambda: self._run_publish(
            layer=layer,
            profile=profile,
            profile_name=profile_name,
            title=title,
            description=description,
            access=access,
        ))

    def _run_publish(
        self,
        *,
        layer: QgsVectorLayer,
        profile,
        profile_name: str,
        title: str,
        description: str | None,
        access: str,
    ) -> None:
        try:
            gpkg_path = _export_to_geopackage(layer)
        except Exception as e:  # pragma: no cover -- defensive
            _log.exception("export-to-geopackage failed")
            QMessageBox.critical(self, "Export failed", str(e))
            self._set_busy(False)
            return

        self._progress_label.setText("Uploading to portal (stage)...")
        try:
            staged = _run(_stage_upload(profile=profile, gpkg_path=gpkg_path))
        except Exception as e:
            _log.exception("stage-upload failed")
            QMessageBox.critical(self, "Upload failed", str(e))
            self._set_busy(False)
            _safe_unlink(gpkg_path)
            return

        # The stage response carries one source layer (we exported a
        # single QGIS layer to a single-layer GeoPackage). Translate
        # it to a v3 envelope and create the item.
        if not staged.layers:
            QMessageBox.critical(
                self,
                "Publish failed",
                "Portal probe returned no layers from the uploaded file. "
                "Check the layer's geometry validity in QGIS and retry.",
            )
            self._set_busy(False)
            _safe_unlink(gpkg_path)
            return

        v3_layer = layer_from_probe(probe_layer=staged.layers[0].model_dump(by_alias=True))
        envelope = build_data_layer_envelope(layers=[v3_layer])

        self._progress_label.setText("Creating portal item...")
        try:
            item = _run(_create_item(
                profile=profile,
                title=title,
                description=description,
                envelope=envelope,
                access=access,
            ))
        except Exception as e:
            _log.exception("item-create failed")
            QMessageBox.critical(self, "Publish failed", str(e))
            self._set_busy(False)
            _safe_unlink(gpkg_path)
            return

        self._current_item_id = item.id
        self._current_layer_id = v3_layer.id
        self._current_profile_name = profile_name

        # Enqueue the import job and start polling.
        self._progress_label.setText("Enqueuing import job...")
        try:
            job = _run(_enqueue_job(
                profile=profile,
                item_id=item.id,
                layer_id=v3_layer.id,
                staging_id=staged.staging_id,
                source_layer_name=staged.layers[0].name,
            ))
        except Exception as e:
            _log.exception("enqueue-job failed")
            QMessageBox.critical(self, "Publish failed", str(e))
            self._set_busy(False)
            _safe_unlink(gpkg_path)
            return

        self._current_job = job
        # The local tempfile is no longer needed; the portal has its
        # own copy under /tmp/gg-staging/<id>/ until the job finishes.
        _safe_unlink(gpkg_path)

        # Kick off the poll loop.
        self._progress_bar.setVisible(True)
        self._render_job_progress(job)
        self._start_polling()

    def _start_polling(self) -> None:
        if self._poll_timer is not None:
            return
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_once)
        self._poll_timer.start()

    def _stop_polling(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
            self._poll_timer = None

    def _poll_once(self) -> None:
        if self._current_job is None or self._current_profile_name is None:
            self._stop_polling()
            return
        profile = self._store.get(self._current_profile_name)
        if profile is None or not profile.is_discovered:
            self._stop_polling()
            return
        try:
            fresh = _run(_get_job(profile, self._current_job.id))
        except Exception as e:  # pragma: no cover -- defensive
            _log.exception("poll job failed")
            self._stop_polling()
            self._progress_label.setText(f"Polling error: {e}")
            self._set_busy(False)
            return

        self._current_job = fresh
        self._render_job_progress(fresh)
        if fresh.is_terminal:
            self._stop_polling()
            self._on_job_finished(fresh)

    def _render_job_progress(self, job: ImportJob) -> None:
        pct = job.percent_complete
        if pct is None:
            self._progress_bar.setRange(0, 0)  # indeterminate
            self._progress_label.setText(
                f"Import {job.status}... ({job.processed_features} processed)"
            )
        else:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(int(pct * 100))
            self._progress_label.setText(
                f"Import {job.status}: "
                f"{job.processed_features} / {job.total_features}"
            )

    def _on_job_finished(self, job: ImportJob) -> None:
        if job.status == "succeeded":
            QMessageBox.information(
                self,
                "Published",
                f"Layer published successfully.\n\n"
                f"Inserted: {job.inserted_features}\n"
                f"Item id: {self._current_item_id}",
            )
            self.accept()
            return
        if job.status == "failed":
            QMessageBox.critical(
                self,
                "Import failed",
                f"The portal worker reported an error:\n\n{job.error_message or 'unknown'}",
            )
        elif job.status == "cancelled":
            QMessageBox.warning(
                self,
                "Import cancelled",
                "Import was cancelled. The portal item was created "
                "but contains no features.",
            )
        self._set_busy(False)

    def _on_cancel(self) -> None:
        # If a job is running, ask the portal to cancel it before we
        # close the dialog so the worker doesn't keep churning.
        if (
            self._current_job is not None
            and not self._current_job.is_terminal
            and self._current_profile_name is not None
        ):
            profile = self._store.get(self._current_profile_name)
            if profile is not None and profile.is_discovered:
                try:
                    _run(_cancel_job(profile, self._current_job.id))
                except Exception:
                    _log.exception("cancel job failed")
        self._stop_polling()
        self.reject()

    def _set_busy(self, busy: bool) -> None:
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not busy)
        self._layer_combo.setEnabled(not busy)
        self._connection_combo.setEnabled(not busy)
        self._title_input.setEnabled(not busy)
        self._description_input.setEnabled(not busy)
        self._access_combo.setEnabled(not busy)


# -----------------------------------------------------------
# QGIS-side helpers
# -----------------------------------------------------------


def _summary_from_layer(layer: QgsVectorLayer) -> LayerSummary:
    """Translate a QgsVectorLayer into the validator's input shape."""
    geom_type = QgsWkbTypes.displayString(int(layer.wkbType()))
    crs: QgsCoordinateReferenceSystem = layer.crs()
    crs_auth = crs.authid() if crs.isValid() else ""
    fields = [str(f.name()) for f in layer.fields()]
    return LayerSummary(
        name=layer.name(),
        feature_count=int(layer.featureCount()),
        geometry_type=geom_type,
        crs_auth_id=crs_auth,
        is_valid=bool(layer.isValid()),
        field_names=fields,
    )


def _export_to_geopackage(layer: QgsVectorLayer) -> str:
    """Write the layer out to a temporary single-layer GeoPackage.

    GeoPackage preserves CRS + attribute types more faithfully than
    GeoJSON and the portal's ingest pipeline handles it natively.
    """
    fd, path = tempfile.mkstemp(suffix=".gpkg", prefix="gratisgis-publish-")
    os.close(fd)

    # Try the modern QgsVectorFileWriter API first (5.x+), fall back
    # to the legacy one for older QGIS builds. The legacy API stays
    # available but is deprecated; supporting both keeps us
    # compatible with QGIS 3.16 LTR.
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer.name()
    options.fileEncoding = "UTF-8"

    transform_context = QgsCoordinateTransformContext()
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, transform_context, options
    )
    if err != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"GeoPackage write failed: {msg or err}")
    return path


def _safe_unlink(path: str) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        os.unlink(path)


# -----------------------------------------------------------
# Async bridges -- thin closures that the sync helper in
# browser/fetch.py can drive on a fresh event loop.
# -----------------------------------------------------------


async def _stage_upload(*, profile, gpkg_path):
    async with _connected_client(profile) as client:
        return await client.ingest.stage(file_path=gpkg_path)


async def _create_item(*, profile, title, description, envelope, access):
    async with _connected_client(profile) as client:
        return await client.items.create(
            type="data_layer",
            title=title,
            description=description,
            data=envelope,
            access=access,
        )


async def _enqueue_job(*, profile, item_id, layer_id, staging_id, source_layer_name):
    async with _connected_client(profile) as client:
        return await client.import_jobs.enqueue(
            item_id=item_id,
            layer_id=layer_id,
            staging_id=staging_id,
            source_layer_name=source_layer_name,
            mode="replace",
        )


async def _get_job(profile, job_id: str):
    async with _connected_client(profile) as client:
        return await client.import_jobs.get(job_id)


async def _cancel_job(profile, job_id: str):
    async with _connected_client(profile) as client:
        return await client.import_jobs.cancel(job_id)
