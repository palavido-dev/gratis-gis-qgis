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

The publish flow:

  1. User picks a file from disk.
  2. Pre-flight validation (file_flavor + size).
  3. Create an empty tile_layer item via items.create.
  4. (PMTiles / MBTiles): check-space, presign-upload, PUT to MinIO,
     then finalize so the portal reads the header.
  5. Show success with the new item id.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
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

from ..browser.fetch import _connected_client, _run
from ..log import get_logger
from ..publish.raster import file_flavor, validate_raster_upload
from ..settings import ConnectionStore

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)


class PublishRasterDialog(QDialog):
    """Modal dialog driving the raster publish."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish raster / tile layer to GratisGIS")
        self.setMinimumWidth(560)
        self._iface = iface
        self._store = ConnectionStore()
        self._file_path: str = ""

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
        self._progress_bar.setVisible(False)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Publish")
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
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
            self._issues_list.addItem(row)
            return
        for issue in issues:
            marker = "[ERROR]" if issue.is_error else "[warn]"
            row = QListWidgetItem(f"{marker} {issue.message}")
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
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

        self._buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._progress_label.setText("Creating tile_layer item...")

        try:
            item = _run(_create_tile_item(
                profile=profile,
                title=title,
                description=description,
                access=access,
            ))
        except Exception as e:
            _log.exception("tile_layer item create failed")
            QMessageBox.critical(self, "Publish failed", str(e))
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return

        # Best-effort disk-space check.
        self._progress_label.setText("Checking portal disk space...")
        try:
            space = _run(_check_space(
                profile=profile,
                file_name=os.path.basename(self._file_path),
                size_bytes=size,
            ))
            if not space.ok:
                QMessageBox.critical(
                    self,
                    "Not enough portal space",
                    space.reason or "Portal reports insufficient disk.",
                )
                self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
                self._progress_bar.setVisible(False)
                return
        except Exception:
            # Fail-open: real PUT will surface a hard failure if
            # there's truly no room.
            _log.exception("check-space failed (fail-open)")

        # Mint a presigned PUT URL.
        self._progress_label.setText("Requesting presigned upload URL...")
        try:
            presigned = _run(_presign(profile=profile))
        except Exception as e:
            _log.exception("presign failed")
            QMessageBox.critical(self, "Publish failed", str(e))
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return
        if size > presigned.max_bytes:
            QMessageBox.critical(
                self,
                "Too large",
                f"File is {size / 1024 / 1024:.1f} MB; portal allows "
                f"{presigned.max_bytes / 1024 / 1024 / 1024:.1f} GB per file.",
            )
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return

        # PUT bytes directly to MinIO. This blocks the event loop;
        # for a CLI plugin that's acceptable -- the user picks a
        # file and waits for the result. A chunked-progress
        # version is a stretch goal.
        self._progress_label.setText("Uploading to portal storage...")
        try:
            _upload_to_presigned(presigned.upload_url, self._file_path)
        except Exception as e:
            _log.exception("PUT failed")
            QMessageBox.critical(self, "Upload failed", str(e))
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return

        # Finalize: tells the portal to read the header and
        # populate item.data.
        self._progress_label.setText("Finalizing tile layer...")
        storage_key = f"item-tile-layer/{presigned.key}"
        try:
            _run(_finalize(
                profile=profile,
                item_id=item.id,
                storage_key=storage_key,
                storage_url=presigned.public_url,
                file_name=os.path.basename(self._file_path),
                size_bytes=size,
            ))
        except Exception as e:
            _log.exception("finalize failed")
            QMessageBox.critical(self, "Finalize failed", str(e))
            self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
            self._progress_bar.setVisible(False)
            return

        post_note = ""
        if classification.needs_server_conversion:
            post_note = (
                "\n\nServer-side conversion to PMTiles is queued; the tile "
                "layer will become viewable in a few minutes."
            )
        QMessageBox.information(
            self,
            "Published",
            f"Tile layer '{title}' created.\n\nItem id: {item.id}{post_note}",
        )
        self.accept()


# -----------------------------------------------------------
# Async bridges
# -----------------------------------------------------------


async def _create_tile_item(*, profile, title, description, access):
    async with _connected_client(profile) as client:
        return await client.items.create(
            type="tile_layer",
            title=title,
            description=description,
            data={"version": 1, "processingState": "uploading"},
            access=access,
        )


async def _check_space(*, profile, file_name, size_bytes):
    async with _connected_client(profile) as client:
        return await client.storage.check_tile_layer_space(
            file_name=file_name, size_bytes=size_bytes
        )


async def _presign(*, profile):
    async with _connected_client(profile) as client:
        return await client.storage.presign_upload(
            kind="item-tile-layer", content_type="application/octet-stream"
        )


async def _finalize(*, profile, item_id, storage_key, storage_url, file_name, size_bytes):
    async with _connected_client(profile) as client:
        return await client.tile_layer.finalize(
            item_id=item_id,
            storage_key=storage_key,
            storage_url=storage_url,
            file_name=file_name,
            size_bytes=size_bytes,
        )


def _upload_to_presigned(upload_url: str, file_path: str) -> None:
    """PUT the file bytes to MinIO via the presigned URL.

    Sync httpx call so it can be driven from the Qt event loop
    without spinning the asyncio loop. 30-minute timeout because
    a multi-GB GeoTIFF on a slow link genuinely takes a while.
    """
    with open(file_path, "rb") as fh:
        response = httpx.put(
            upload_url,
            content=fh.read(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx.Timeout(1800.0, connect=10.0),
        )
    if response.status_code >= 300:
        raise RuntimeError(
            f"PUT to storage failed: HTTP {response.status_code} {response.text[:200]}"
        )
