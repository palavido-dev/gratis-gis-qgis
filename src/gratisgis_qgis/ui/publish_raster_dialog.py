# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish-raster dialog (Phase 5).

Uploads a PMTiles / MBTiles / GeoTIFF / COG / JP2 file as a portal
tile_layer item. The portal does any server-side conversion
(MBTiles -> PMTiles, raw raster -> COG -> PMTiles).

We deliberately don't try to convert raster -> PMTiles in the
plugin. That would require an external CLI binary (tippecanoe /
pmtiles) which we can't reliably bundle in a QGIS plugin. Users
who want PMTiles directly should convert externally and upload
the .pmtiles file; everyone else uploads raw GeoTIFF and lets the
portal do the work.

The publish flow, all inside ONE background task so a multi-GB
upload never blocks the GUI thread:

  1. Create an empty tile_layer item via items.create.
  2. Best-effort disk-space check (fail-open).
  3. Presign the upload, declaring the file size so the portal can
     enforce its size cap before any bytes move.
  4. PUT the file to MinIO, streamed from disk in chunks (constant
     memory) with live progress and cancel.
  5. Finalize so the portal reads the header.

The PUT goes through the client's transport seam rather than any
HTTP library of its own, with the connection profile's TLS-verify
setting, so portals on self-signed certificates behave the same
here as on every other call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

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
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gratisgis_client.transport import TransportRequest, UrllibTransport

from ..log import get_logger
from ..portal import get_client
from ..publish.raster import file_flavor, validate_raster_upload
from ..settings import ConnectionStore
from ..tasks import (
    TaskCancelledError,
    TaskController,
    TaskHandle,
    format_error,
    run_in_task,
)

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)

# 30-minute socket timeout on the PUT because a multi-GB GeoTIFF on
# a slow link genuinely takes a while; the connect itself still
# fails fast via the transport's normal error path.
_UPLOAD_TIMEOUT = 1800.0

# Progress budget: the four metadata calls bracket the upload, which
# gets the wide middle band because it is where the wall-clock goes.
_PCT_ITEM_CREATED = 2.0
_PCT_UPLOAD_START = 6.0
_PCT_UPLOAD_END = 96.0


@dataclass(frozen=True)
class _PublishOutcome:
    """What the pipeline hands back to the GUI callback."""

    item_id: str
    needs_server_conversion: bool


class PublishRasterDialog(QDialog):
    """Modal dialog driving the raster publish."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish raster / tile layer to GratisGIS")
        self.setMinimumWidth(560)
        self._iface = iface
        self._store = ConnectionStore()
        self._file_path: str = ""
        self._task: TaskController | None = None
        # Set on reject: a pipeline finishing right as the user
        # dismissed the dialog must not pop a floating result box.
        self._closed = False

        self._file_label = QLabel("(no file selected)")
        self._file_button = QPushButton("Choose file...")
        self._file_button.clicked.connect(self._on_pick_file)

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
        form.addRow("File:", self._file_label)
        form.addRow("", self._file_button)
        form.addRow("Portal:", self._connection_combo)
        form.addRow("Title:", self._title_input)
        form.addRow("Description:", self._description_input)
        form.addRow("Access:", self._access_combo)

        self._issues_list = QListWidget()
        self._issues_list.setFixedHeight(100)

        self._progress_label = QLabel("")
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setVisible(False)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Publish")
        buttons.accepted.connect(self._on_publish)
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

    def _populate_connection_combo(self) -> None:
        for name in self._store.list_names():
            profile = self._store.get(name)
            if profile is None or not profile.is_discovered:
                continue
            self._connection_combo.addItem(profile.display_label, userData=name)
        if self._connection_combo.count() == 0:
            self._connection_combo.addItem("(no signed-in connections)", None)
            self._connection_combo.setEnabled(False)

    def _on_pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a raster / tile file",
            "",
            "Tile layer files (*.pmtiles *.mbtiles *.tif *.tiff *.geotiff *.cog *.jp2);;All files (*)",
        )
        if not path:
            return
        self._file_path = path
        self._file_label.setText(os.path.basename(path))
        if not self._title_input.text().strip():
            self._title_input.setText(os.path.splitext(os.path.basename(path))[0])
        self._render_validation()

    def _render_validation(self) -> None:
        self._issues_list.clear()
        if not self._file_path:
            self._issues_list.addItem(QListWidgetItem("Pick a file first."))
            return
        try:
            size = os.path.getsize(self._file_path)
        except OSError as e:
            self._issues_list.addItem(QListWidgetItem(f"Stat failed: {e}"))
            return
        issues = validate_raster_upload(
            file_path=self._file_path,
            size_bytes=size,
        )
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

    def _on_publish(self) -> None:
        if not self._file_path:
            QMessageBox.warning(self, "No file selected", "Choose a file first.")
            return
        profile_name = self._connection_combo.currentData()
        if not profile_name:
            QMessageBox.warning(self, "No connection", "Pick a signed-in connection.")
            return
        try:
            size = os.path.getsize(self._file_path)
        except OSError as e:
            QMessageBox.critical(self, "File error", str(e))
            return

        issues = validate_raster_upload(file_path=self._file_path, size_bytes=size)
        blocking = [i for i in issues if i.is_error]
        if blocking:
            QMessageBox.critical(
                self,
                "Validation failed",
                "Fix the following before publishing:\n\n"
                + "\n".join(f"- {i.message}" for i in blocking),
            )
            return

        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            QMessageBox.warning(self, "Connection not ready", "Sign in first.")
            return

        title = self._title_input.text().strip() or os.path.basename(self._file_path)
        description = self._description_input.toPlainText().strip() or None
        access = self._access_combo.currentData() or "private"
        classification = file_flavor(self._file_path)
        file_path = self._file_path
        file_name = os.path.basename(file_path)

        self._set_busy(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(0)
        self._progress_label.setText("Publishing tile layer...")

        # Filled by the worker when a post-create failure (or cancel)
        # triggered orphan cleanup; read by the error callback so the
        # user hears what happened to the half-created item.
        cleanup_notes: list[str] = []

        def pipeline(handle: TaskHandle) -> _PublishOutcome:
            return _publish_pipeline(
                handle,
                profile=profile,
                file_path=file_path,
                file_name=file_name,
                size=size,
                title=title,
                description=description,
                access=access,
                needs_server_conversion=classification.needs_server_conversion,
                cleanup_notes=cleanup_notes,
            )

        def done(outcome: _PublishOutcome) -> None:
            self._task = None
            if self._closed:
                _log.info("tile layer %s published after the dialog closed", outcome.item_id)
                return
            post_note = ""
            if outcome.needs_server_conversion:
                post_note = (
                    "\n\nServer-side conversion to PMTiles is queued; the tile "
                    "layer will become viewable in a few minutes."
                )
            QMessageBox.information(
                self,
                "Published",
                f"Tile layer '{title}' created.\n\nItem id: {outcome.item_id}{post_note}",
            )
            self.accept()

        def failed(exc: BaseException) -> None:
            self._task = None
            self._set_busy(False)
            self._progress_bar.setVisible(False)
            note = " ".join(cleanup_notes)
            if isinstance(exc, TaskCancelledError):
                text = "Publish cancelled."
                if note:
                    text = f"{text} {note}"
                self._progress_label.setText(text)
                return
            _log.error("raster publish failed", exc_info=exc)
            if self._closed:
                return
            self._progress_label.setText("")
            message = format_error(exc)
            if note:
                message = f"{message}\n\n{note}"
            QMessageBox.critical(self, "Publish failed", message)

        self._task = run_in_task(
            f"GratisGIS: publish {file_name}",
            pipeline,
            done,
            failed,
            on_progress=lambda pct: self._progress_bar.setValue(int(pct)),
        )

    def reject(self) -> None:  # Qt override
        # Cancel button / Esc / window close while the pipeline runs:
        # request cancellation so the upload aborts at the next chunk
        # instead of finishing headless after the dialog is gone.
        self._closed = True
        if self._task is not None:
            self._task.cancel()
        super().reject()

    def _set_busy(self, busy: bool) -> None:
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not busy)
        self._file_button.setEnabled(not busy)
        self._connection_combo.setEnabled(not busy)
        self._title_input.setEnabled(not busy)
        self._description_input.setEnabled(not busy)
        self._access_combo.setEnabled(not busy)


# -----------------------------------------------------------
# Worker-side pipeline (no Qt in here)
# -----------------------------------------------------------


def _publish_pipeline(
    handle: TaskHandle,
    *,
    profile,
    file_path: str,
    file_name: str,
    size: int,
    title: str,
    description: str | None,
    access: str,
    needs_server_conversion: bool,
    cleanup_notes: list[str],
) -> _PublishOutcome:
    """Create + check-space + presign + upload + finalize, in order.

    Raises on any hard failure; the dialog's error callback renders
    it. Any exit after the item create that is not a successful
    finalize (failure or cancel alike) would strand an empty
    tile_layer item, so the created item is deleted best-effort and
    the outcome recorded in ``cleanup_notes`` for the dialog's error
    surface. The original exception always propagates unchanged so
    cancels still present as cancels.
    """
    client = get_client(profile)

    item = client.items.create(
        type="tile_layer",
        title=title,
        description=description,
        data={"version": 1, "processingState": "uploading"},
        access=access,
    )
    try:
        handle.set_progress(_PCT_ITEM_CREATED)
        _raise_if_canceled(handle)

        # Best-effort disk-space check: a portal-side refusal is a
        # hard stop with its reason, but an errored check falls open
        # and lets the real PUT surface any genuine shortage.
        try:
            space = client.storage.check_tile_layer_space(
                file_name=file_name, size_bytes=size
            )
        except Exception:
            _log.exception("check-space failed (fail-open)")
        else:
            if not space.ok:
                raise RuntimeError(space.reason or "Portal reports insufficient disk.")
        _raise_if_canceled(handle)

        # Declaring the size lets the portal refuse oversized files at
        # presign time and bakes Content-Length into the signature; the
        # PUT below must therefore send exactly this length.
        presigned = client.storage.presign_upload(
            kind="item-tile-layer",
            content_type="application/octet-stream",
            size_bytes=size,
        )
        if presigned.max_bytes and size > presigned.max_bytes:
            raise RuntimeError(
                f"File is {size / 1024 / 1024:.1f} MB; portal allows "
                f"{presigned.max_bytes / 1024 / 1024 / 1024:.1f} GB per file."
            )
        handle.set_progress(_PCT_UPLOAD_START)
        _raise_if_canceled(handle)

        _upload_to_presigned(
            presigned.upload_url,
            file_path,
            size=size,
            verify_tls=profile.verify_tls,
            handle=handle,
        )
        handle.set_progress(_PCT_UPLOAD_END)
        _raise_if_canceled(handle)

        storage_key = f"item-tile-layer/{presigned.key}"
        client.tile_layer.finalize(
            item_id=item.id,
            storage_key=storage_key,
            storage_url=presigned.public_url,
            file_name=file_name,
            size_bytes=size,
        )
    except BaseException:
        if _delete_item_quietly(client, item.id):
            cleanup_notes.append(
                "The partly created tile layer item was removed from the portal."
            )
        else:
            cleanup_notes.append(
                f"A partly created tile layer item ({item.id}) could not be "
                "removed; delete it in the portal if it appears."
            )
        raise
    handle.set_progress(100.0)
    return _PublishOutcome(
        item_id=item.id, needs_server_conversion=needs_server_conversion
    )


def _delete_item_quietly(client, item_id: str) -> bool:
    """Best-effort delete for orphan cleanup; never raises."""
    try:
        client.items.delete(item_id)
    except Exception:
        _log.exception("cleanup delete of item %s failed", item_id)
        return False
    return True


def _raise_if_canceled(handle: TaskHandle) -> None:
    if handle.is_canceled():
        raise TaskCancelledError("Publish cancelled")


class _ProgressFileReader:
    """File wrapper that reports upload progress and honors cancel.

    urllib drains the request body through ``read(n)``, so metering
    here observes exactly what went onto the socket buffer. Raising
    from ``read`` is also the only reliable way to abort an in-flight
    urllib upload; the transport surfaces it to the task as-is.
    """

    def __init__(self, fh: IO[bytes], total: int, handle: TaskHandle) -> None:
        self._fh = fh
        self._total = max(1, total)
        self._sent = 0
        self._handle = handle

    def read(self, n: int = -1) -> bytes:
        if self._handle.is_canceled():
            raise TaskCancelledError("Upload cancelled")
        chunk = self._fh.read(n)
        self._sent += len(chunk)
        span = _PCT_UPLOAD_END - _PCT_UPLOAD_START
        self._handle.set_progress(
            _PCT_UPLOAD_START + span * min(1.0, self._sent / self._total)
        )
        return chunk


def _upload_to_presigned(
    upload_url: str,
    file_path: str,
    *,
    size: int,
    verify_tls: bool,
    handle: TaskHandle,
) -> None:
    """PUT the file bytes to MinIO via the presigned URL.

    Streams from disk in chunks (constant memory however large the
    raster) through the client's transport seam. The explicit
    Content-Length is load-bearing twice over: without it urllib
    switches to chunked transfer encoding, which S3-style endpoints
    reject, and the presigned signature covers the declared length.
    ``verify_tls`` carries the connection profile's setting so
    portals on self-signed certificates work here too.
    """
    transport = UrllibTransport(verify_tls=verify_tls)
    with open(file_path, "rb") as fh:
        response = transport.send(
            TransportRequest(
                method="PUT",
                url=upload_url,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                },
                body=_ProgressFileReader(fh, size, handle),
                timeout=_UPLOAD_TIMEOUT,
            )
        )
    if response.status >= 300:
        raise RuntimeError(
            f"PUT to storage failed: HTTP {response.status} {response.text[:200]}"
        )
