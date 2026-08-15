# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish-vector-layer dialog (Phase 3).

The user picks a QGIS vector layer, the dialog runs pre-flight
validation, exports the layer to a GeoPackage tempfile, stages
the upload on the portal, creates a new ``data_layer`` item, and
enqueues an import job. While the worker imports, the dialog polls
the job status and renders progress.

QGIS interaction lives here so the translation logic in
``publish/vector.py`` stays free of QGIS imports and testable
without QGIS.

Threading: the GeoPackage export stays on the GUI thread because it
reads the live ``QgsVectorLayer`` (not safe to touch from a worker),
but every network step (stage upload, item create + job enqueue,
each status poll, job cancel) runs in a background task so a big
upload never freezes QGIS.
"""
from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

from qgis.core import (  # type: ignore[import-not-found]
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsProject,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import Qt, QTimer  # type: ignore[import-not-found]
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
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gratisgis_client.endpoints.import_jobs import ImportJob

from ..log import get_logger
from ..portal import get_client
from ..publish.raster import validate_raster_upload
from ..publish.source import (
    PublishChoice,
    remember_published_item,
    resolve_raster_source,
)
from ..publish.vector import LayerSummary, validate_layer
from ..publish.vector_pipeline import PCT_UPLOAD_DONE, run_vector_pipeline
from ..qgis_compat import resolve_enum
from ..settings import ConnectionStore
from ..tasks import format_error, run_in_task
from .publish_raster_dialog import run_raster_pipeline

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)

# How often to poll the import-jobs endpoint while a job is
# running. The portal worker flushes batch metrics every ~500 ms,
# so 1 s strikes a good balance between snappiness and load.
_POLL_INTERVAL_MS = 1000


class PublishLayerDialog(QDialog):
    """Modal dialog driving the vector-publish flow."""

    def __init__(
        self,
        iface: QgisInterface,
        parent: QWidget | None = None,
        *,
        preselect_layer_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish layer to GratisGIS")
        self.setMinimumWidth(560)
        self._iface = iface
        self._store = ConnectionStore()
        self._preselect_layer_id = preselect_layer_id
        # Everything offered in the picker, in combo order. The combo
        # holds an index into this rather than a layer id, because an
        # entry can also be a file that is not a layer at all.
        self._choices: list[PublishChoice] = []
        # Set while a raster upload is running, so closing the dialog
        # can stop it rather than leave it running headless.
        self._task = None
        self._poll_timer: QTimer | None = None
        # True while a poll task is in flight, so a slow poll makes
        # the timer skip ticks instead of stacking requests.
        self._poll_in_flight = False
        # Set on reject: with a live event loop the user can dismiss
        # the dialog mid-flow, and completion callbacks landing after
        # that must stop the pipeline instead of publishing headless.
        self._closed = False
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
        # Whether the user has typed their own title. Until they do,
        # the box follows the selected layer.
        self._title_touched = False
        self._title_input.textEdited.connect(self._on_title_edited)
        self._description_input = QPlainTextEdit()
        self._description_input.setFixedHeight(60)
        self._access_combo = QComboBox()
        self._access_combo.addItem("Private (only you)", "private")
        self._access_combo.addItem("Org (everyone in your org)", "org")
        self._access_combo.addItem("Public (anyone with the link)", "public")

        self._file_button = QPushButton("Choose a file instead...")
        self._file_button.clicked.connect(self._on_pick_file)

        form = QFormLayout()
        form.addRow("Layer:", self._layer_combo)
        form.addRow("", self._file_button)
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
        buttons.rejected.connect(self.reject)
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
        """Offer everything in the project that could be published.

        Vector layers become data layers and raster layers become tile
        layers, so both belong in one list: a user thinking "I need to
        publish this" should not first have to work out which of two
        menu entries their layer counts as.

        Rasters that cannot be published (a web service, a file that
        has moved) are listed anyway rather than hidden. Being told why
        the aerial you are looking at is not offered is far better than
        wondering where it went.

        isinstance rather than a layer-type constant: QGIS 4 retired
        the QgsMapLayer.VectorLayer integer in favour of
        Qgis.LayerType.Vector, while the classes are stable on both.
        """
        self._layer_combo.clear()
        self._choices = []
        project = QgsProject.instance()
        preselect_idx = -1

        for layer_id, layer in project.mapLayers().items():
            if isinstance(layer, QgsVectorLayer):
                choice = PublishChoice(
                    kind="vector", label=layer.name(), layer_id=layer_id
                )
            elif isinstance(layer, QgsRasterLayer):
                choice = _raster_choice(layer, layer_id)
            else:
                continue
            self._choices.append(choice)
            suffix = "" if choice.is_publishable else "  (cannot be published)"
            self._layer_combo.addItem(
                f"{choice.label}{suffix}", userData=len(self._choices) - 1
            )
            if (
                self._preselect_layer_id is not None
                and layer_id == self._preselect_layer_id
            ):
                preselect_idx = self._layer_combo.count() - 1

        if self._layer_combo.count() == 0:
            self._layer_combo.addItem("(no layers in project)", None)
            self._layer_combo.setEnabled(False)
        elif preselect_idx >= 0:
            self._layer_combo.setCurrentIndex(preselect_idx)

    def _on_pick_file(self) -> None:
        """Publish a file that is not in the project.

        The escape hatch for the raster sitting on disk that the user
        has not added to their map, which the old file-only dialog
        made the default and the only way.
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a file to publish",
            "",
            "Raster / tile files (*.tif *.tiff *.geotiff *.cog *.jp2 "
            "*.pmtiles *.mbtiles);;All files (*)",
        )
        if not path:
            return
        self._choices.append(
            PublishChoice(
                kind="file", label=os.path.basename(path), file_path=path
            )
        )
        self._layer_combo.setEnabled(True)
        self._layer_combo.addItem(
            f"{os.path.basename(path)}  (file on disk)",
            userData=len(self._choices) - 1,
        )
        self._layer_combo.setCurrentIndex(self._layer_combo.count() - 1)

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
        """Follow the selection, unless the user has typed a title.

        The title has to track the chosen layer rather than only fill
        when blank. Project layers arrive in no particular order, so
        the box was pre-filled from whichever happened to be first and
        then kept that name after the user picked something else,
        which is a good way to publish under the wrong title without
        noticing.

        ``textEdited`` rather than ``textChanged`` is what makes this
        safe: it fires only for typing, so the dialog's own setText
        below cannot mark the field as user-owned.
        """
        choice = self._selected_choice()
        if choice is None:
            if not self._title_touched:
                self._title_input.setText("")
            return
        if not self._title_touched:
            stem = choice.label
            if choice.kind == "file":
                stem = os.path.splitext(choice.label)[0]
            self._title_input.setText(stem)
        self._on_validate()

    def _on_title_edited(self, _text: str) -> None:
        self._title_touched = True

    # ----- Validation -----

    def _on_validate(self) -> None:
        self._issues_list.clear()
        choice = self._selected_choice()
        if choice is None:
            self._issues_list.addItem(QListWidgetItem("Select a layer first."))
            return
        if not choice.is_publishable:
            self._add_issue("[ERROR]", choice.reason)
            return
        if choice.kind == "vector":
            layer = self._selected_vector_layer()
            if layer is None:
                self._add_issue("[ERROR]", "That layer is no longer in the project.")
                return
            issues = validate_layer(_summary_from_layer(layer))
        else:
            path = self._selected_file_path()
            if not path:
                self._add_issue("[ERROR]", "That file is no longer available.")
                return
            try:
                size = os.path.getsize(path)
            except OSError as e:
                self._add_issue("[ERROR]", f"The file could not be read: {e}")
                return
            issues = validate_raster_upload(file_path=path, size_bytes=size)

        if not issues:
            self._add_issue("", "All checks passed.")
            return
        for issue in issues:
            self._add_issue("[ERROR]" if issue.is_error else "[warn]", issue.message)

    def _add_issue(self, marker: str, message: str) -> None:
        row = QListWidgetItem(f"{marker} {message}".strip())
        row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._issues_list.addItem(row)

    def _selected_choice(self) -> PublishChoice | None:
        index = self._layer_combo.currentData()
        if index is None or not isinstance(index, int):
            return None
        if 0 <= index < len(self._choices):
            return self._choices[index]
        return None

    def _selected_vector_layer(self) -> QgsVectorLayer | None:
        choice = self._selected_choice()
        if choice is None or choice.kind != "vector":
            return None
        layer = QgsProject.instance().mapLayer(choice.layer_id)
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _selected_file_path(self) -> str:
        """The file behind the selection, for either raster route.

        A project raster and a file picked from disk end up in the same
        place, which is the point of the merge: past this line the two
        are the same publish.
        """
        choice = self._selected_choice()
        if choice is None or choice.kind == "vector":
            return ""
        return choice.file_path if os.path.isfile(choice.file_path) else ""

    # ----- Publish flow -----

    def _on_publish(self) -> None:
        choice = self._selected_choice()
        if choice is None:
            QMessageBox.warning(self, "No layer selected", "Pick a layer to publish.")
            return
        if not choice.is_publishable:
            QMessageBox.critical(self, "Cannot publish this layer", choice.reason)
            return
        if choice.kind != "vector":
            self._publish_raster(choice)
            return

        layer = self._selected_vector_layer()
        if layer is None:
            QMessageBox.warning(
                self, "No layer selected", "That layer is no longer in the project."
            )
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

        self._set_busy(True)
        self._progress_label.setText("Exporting layer to GeoPackage...")
        # Deferred one tick so the label paints before the export
        # (which reads the live layer and must stay on this thread)
        # briefly blocks.
        QTimer.singleShot(0, lambda: self._start_publish(
            layer=layer,
            profile=profile,
            profile_name=profile_name,
            title=title,
            description=description,
            access=access,
        ))

    def _publish_raster(self, choice: PublishChoice) -> None:
        """Hand a raster off to the tile-layer upload.

        Both raster routes converge here: a layer already on the canvas
        and a file chosen from disk differ only in how their path was
        found. The upload itself is the raster dialog's pipeline, used
        rather than reimplemented so the two cannot drift.
        """
        path = self._selected_file_path()
        if not path:
            QMessageBox.critical(
                self,
                "File not found",
                "The file behind that layer is no longer on this computer.",
            )
            return
        profile_name = self._connection_combo.currentData()
        if not profile_name:
            QMessageBox.warning(
                self, "No connection selected", "Pick a signed-in connection."
            )
            return
        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            QMessageBox.warning(
                self, "Connection not ready", "Sign in to the connection first."
            )
            return

        try:
            size = os.path.getsize(path)
        except OSError as e:
            QMessageBox.critical(self, "File unreadable", str(e))
            return
        blocking = [
            i
            for i in validate_raster_upload(file_path=path, size_bytes=size)
            if i.is_error
        ]
        if blocking:
            QMessageBox.critical(
                self,
                "Validation failed",
                "Fix the following before publishing:\n\n"
                + "\n".join(f"- {i.message}" for i in blocking),
            )
            return

        self._set_busy(True)
        self._progress_bar.setVisible(True)
        self._progress_label.setText("Uploading to portal...")
        cleanup_notes: list[str] = []
        title = self._title_input.text().strip() or choice.label

        def pipeline(handle):
            return run_raster_pipeline(
                handle,
                profile=profile,
                file_path=path,
                file_name=os.path.basename(path),
                size=size,
                title=title,
                description=self._description_input.toPlainText().strip() or None,
                access=self._access_combo.currentData() or "private",
                needs_server_conversion=not path.lower().endswith(".pmtiles"),
                cleanup_notes=cleanup_notes,
            )

        def done(outcome) -> None:
            self._task = None
            self._set_busy(False)
            self._progress_bar.setVisible(False)
            if self._closed:
                return
            # Remember what this layer became, so publishing the
            # project afterwards recognises it instead of offering to
            # publish the same raster a second time.
            if choice.kind == "raster" and choice.layer_id:
                remember_published_item(
                    QgsProject.instance().mapLayer(choice.layer_id),
                    outcome.item_id,
                )
            QMessageBox.information(
                self,
                "Published",
                f"Layer published successfully.\n\nItem id: {outcome.item_id}\n\n"
                "Conversion runs on the portal and may take a few minutes.",
            )
            self.accept()

        def failed(exc: BaseException) -> None:
            self._task = None
            self._set_busy(False)
            self._progress_bar.setVisible(False)
            _log.error("raster publish failed", exc_info=exc)
            if self._closed:
                return
            extra = ("\n\n" + "\n".join(cleanup_notes)) if cleanup_notes else ""
            QMessageBox.critical(
                self, "Publish failed", f"{format_error(exc)}{extra}"
            )

        def progress(pct: float) -> None:
            self._progress_bar.setValue(int(pct))

        self._task = run_in_task(
            "GratisGIS: publish raster", pipeline, done, failed, on_progress=progress
        )

    def _start_publish(
        self,
        *,
        layer: QgsVectorLayer,
        profile,
        profile_name: str,
        title: str,
        description: str | None,
        access: str,
    ) -> None:
        """Export the layer, then hand the file to the pipeline.

        The export stays here, on the GUI thread, because it needs QGIS
        and because that is where it has always run. Everything after it
        is one call into ``run_vector_pipeline``: what used to be two
        tasks with a GUI hop between them, where the interesting half
        (probe the staged file, build the envelope, create, enqueue,
        clean up an orphan) sat in a method nothing could reach.
        """
        try:
            gpkg_path = _export_to_geopackage(layer)
        except Exception as e:  # pragma: no cover - defensive
            _log.exception("export-to-geopackage failed")
            QMessageBox.critical(self, "Export failed", str(e))
            self._set_busy(False)
            return

        self._progress_label.setText("Uploading to portal...")

        # Filled by the worker when a post-create failure triggered
        # orphan cleanup; read by the error callback so the user
        # hears what happened to the half-created item either way.
        cleanup_notes: list[str] = []

        def pipeline(handle):
            return run_vector_pipeline(
                handle,
                profile=profile,
                gpkg_path=gpkg_path,
                title=title,
                description=description,
                access=access,
                cleanup_notes=cleanup_notes,
            )

        def done(outcome) -> None:
            self._current_item_id = outcome.item_id
            self._current_layer_id = outcome.layer_id
            self._current_profile_name = profile_name
            self._current_job = outcome.job
            if self._closed:
                # The job was already enqueued when the user bailed;
                # ask the worker to stop rather than importing into
                # an item nobody is watching.
                self._request_job_cancel()
                return
            self._progress_bar.setVisible(True)
            self._render_job_progress(outcome.job)
            self._start_polling()

        def failed(exc: BaseException) -> None:
            _log.error("vector publish failed", exc_info=exc)
            if self._closed:
                # Dialog dismissed mid-flight; the staged copy ages out
                # server-side within the hour.
                return
            message = format_error(exc)
            if cleanup_notes:
                message = message + "\n\n" + " ".join(cleanup_notes)
            QMessageBox.critical(self, "Publish failed", message)
            self._set_busy(False)

        def progress(pct: float) -> None:
            # The label is the only progress signal until the import
            # job starts reporting its own; the bar stays hidden until
            # then because staging cannot say how far along it is.
            self._progress_label.setText(
                "Uploading to portal..."
                if pct <= PCT_UPLOAD_DONE
                else "Creating portal item..."
            )

        run_in_task(
            "GratisGIS publish: vector layer",
            pipeline,
            done,
            failed,
            cancelable=False,
            on_progress=progress,
        )

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
        if self._poll_in_flight:
            # Previous poll still on the wire; skip this tick rather
            # than queueing a growing backlog against a slow portal.
            return
        if self._current_job is None or self._current_profile_name is None:
            self._stop_polling()
            return
        profile = self._store.get(self._current_profile_name)
        if profile is None or not profile.is_discovered:
            self._stop_polling()
            return
        job_id = self._current_job.id
        self._poll_in_flight = True

        def poll(_handle):
            return get_client(profile).import_jobs.get(job_id)

        def done(fresh) -> None:
            self._poll_in_flight = False
            if self._poll_timer is None:
                # Dialog was cancelled while this poll was in flight.
                return
            self._current_job = fresh
            self._render_job_progress(fresh)
            if fresh.is_terminal:
                self._stop_polling()
                self._on_job_finished(fresh)

        def failed(exc: BaseException) -> None:
            self._poll_in_flight = False
            _log.error("poll job failed", exc_info=exc)
            self._stop_polling()
            self._reset_after_poll_error(format_error(exc))

        run_in_task("GratisGIS publish: poll status", poll, done, failed, cancelable=False)

    def _reset_after_poll_error(self, error_text: str) -> None:
        """Return the dialog to a publishable state after a poll
        failure.

        The job may or may not still be running server-side, but the
        dialog can no longer observe it. Keeping the half-run state
        around meant the next Publish click created a SECOND item on
        top of the invisible first one, so everything job-related
        resets here (including the progress bar's possible
        indeterminate range) and only the error text stays visible.
        """
        self._current_job = None
        self._current_item_id = None
        self._current_layer_id = None
        self._current_profile_name = None
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_label.setText(f"Polling error: {error_text}")
        self._set_busy(False)

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

    def reject(self) -> None:  # Qt override
        # Covers the Cancel button, Esc, and the window close button
        # alike: if a job is running, ask the portal to cancel it (in
        # a task; the worker should not keep churning) before closing.
        self._closed = True
        self._request_job_cancel()
        self._stop_polling()
        super().reject()

    def _request_job_cancel(self) -> None:
        if (
            self._current_job is None
            or self._current_job.is_terminal
            or self._current_profile_name is None
        ):
            return
        profile = self._store.get(self._current_profile_name)
        if profile is None or not profile.is_discovered:
            return
        job_id = self._current_job.id

        def cancel(_handle):
            return get_client(profile).import_jobs.cancel(job_id)

        def done(_job) -> None:
            _log.debug("import job %s cancelled", job_id)

        def failed(exc: BaseException) -> None:
            _log.error("cancel job failed", exc_info=exc)

        run_in_task("GratisGIS publish: cancel job", cancel, done, failed, cancelable=False)

    def _set_busy(self, busy: bool) -> None:
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not busy)
        self._layer_combo.setEnabled(not busy)
        self._connection_combo.setEnabled(not busy)
        self._title_input.setEnabled(not busy)
        self._description_input.setEnabled(not busy)
        self._access_combo.setEnabled(not busy)


# -----------------------------------------------------------
# Worker-side helpers (no Qt in here)
# -----------------------------------------------------------


# -----------------------------------------------------------
# QGIS-side helpers
# -----------------------------------------------------------


def _raster_choice(layer: QgsRasterLayer, layer_id: str) -> PublishChoice:
    """Describe a project raster: publishable, or why not.

    The provider name is asked for rather than inferred, because a
    tiled web service and a file on disk are told apart by which
    provider is serving them, not by anything in the source string.
    """
    provider = ""
    try:
        provider = layer.dataProvider().name()
    except Exception:
        _log.debug("could not read the raster provider name", exc_info=True)
    resolved = resolve_raster_source(layer.source(), provider)
    return PublishChoice(
        kind="raster",
        label=layer.name(),
        layer_id=layer_id,
        file_path=resolved.file_path,
        reason=resolved.reason,
    )


def _summary_from_layer(layer: QgsVectorLayer) -> LayerSummary:
    """Translate a QgsVectorLayer into the validator's input shape."""
    wkb = layer.wkbType()
    try:
        # QGIS 4 / PyQt6: wkbType() returns the scoped Qgis.WkbType
        # enum and displayString takes exactly that, so the enum goes
        # straight through; the historical int() cast raises
        # TypeError under strict enums. Passing the enum also works
        # on QGIS 3, where sip accepts the int-backed value directly.
        geom_type = QgsWkbTypes.displayString(wkb)
    except TypeError:
        # Older builds where wkbType() came back as a plain int and
        # displayString insists on its own enum type.
        geom_type = QgsWkbTypes.displayString(int(wkb))
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

    # QGIS 3 exposed NoError as a class-level shortcut; the scoped
    # WriterError enum is its home on newer builds and the only
    # spelling under QGIS 4's strict PyQt6.
    no_error = resolve_enum(
        (getattr(QgsVectorFileWriter, "WriterError", None), "NoError"),
        (QgsVectorFileWriter, "NoError"),
    )
    transform_context = QgsCoordinateTransformContext()
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, transform_context, options
    )
    if err != no_error:
        raise RuntimeError(f"GeoPackage write failed: {msg or err}")
    return path


def _safe_unlink(path: str) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        os.unlink(path)
