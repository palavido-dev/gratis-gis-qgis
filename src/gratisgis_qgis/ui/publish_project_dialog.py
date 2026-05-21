# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish-project-as-map dialog (Phase 6).

Walks the active QGIS project, lists which layers will be kept on
the published map vs. skipped (with reasons), captures the canvas
viewport, and on Publish creates a portal `map` item via
`POST /api/items`. The result page shows the new item's URL.

This dialog stays thin: the shape-mapping rules live in
`publish/project_to_map.py` so they're testable without QGIS.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.core import (  # type: ignore[import-not-found]
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
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
    QVBoxLayout,
    QWidget,
)

from gratisgis_client.models.item import ItemSummary

from ..browser.fetch import _connected_client, _run
from ..log import get_logger
from ..publish.project_to_map import (
    CanvasLayer,
    CanvasViewport,
    MapTranslation,
    ProjectSnapshot,
    translate,
)
from ..settings import ConnectionStore

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)


class PublishProjectDialog(QDialog):
    """Modal dialog driving the project-to-map publish."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish QGIS project as GratisGIS map")
        self.setMinimumWidth(520)
        self._iface = iface
        self._store = ConnectionStore()
        self._translation: MapTranslation | None = None

        # ----- Connection picker + map metadata -----
        self._connection_combo = QComboBox()
        self._populate_connection_combo()

        self._title_input = QLineEdit()
        self._title_input.setText(_project_title_or_fallback())
        self._title_input.setMinimumWidth(280)
        self._description_input = QPlainTextEdit()
        self._description_input.setFixedHeight(64)

        form = QFormLayout()
        form.addRow("Portal:", self._connection_combo)
        form.addRow("Map title:", self._title_input)
        form.addRow("Description:", self._description_input)

        # ----- Layer audit -----
        self._included_list = QListWidget()
        self._skipped_list = QListWidget()
        self._summary_label = QLabel("")

        # ----- Buttons -----
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("Publish")
        buttons.accepted.connect(self._on_publish)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons

        # ----- Compose layout -----
        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(QLabel("Layers that will be published:"))
        layout.addWidget(self._included_list, 2)
        layout.addWidget(QLabel("Skipped (not on the portal yet):"))
        layout.addWidget(self._skipped_list, 1)
        layout.addWidget(self._summary_label)
        layout.addWidget(buttons)
        self.setLayout(layout)

        # Populate the lists with the current project state.
        self._snapshot_and_render()

    # ----- Internals -----

    def _populate_connection_combo(self) -> None:
        names = self._store.list_names()
        for name in names:
            profile = self._store.get(name)
            if profile is None or not profile.is_discovered:
                continue
            self._connection_combo.addItem(profile.display_label, userData=name)
        if self._connection_combo.count() == 0:
            self._connection_combo.addItem("(no signed-in connections)", None)
            self._connection_combo.setEnabled(False)

    def _snapshot_and_render(self) -> None:
        snapshot = _build_snapshot(self._iface, title=self._title_input.text())
        result = translate(snapshot)
        self._translation = result

        self._included_list.clear()
        for lyr in result.data.get("layers", []):
            row = QListWidgetItem(
                f"{lyr['title']}  ->  source: {lyr['source']['kind']} ({lyr['source'].get('itemId', '?')})"
            )
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
            self._included_list.addItem(row)

        self._skipped_list.clear()
        for s in result.skipped:
            row = QListWidgetItem(f"{s.name}  [{s.provider}]  -  {s.reason}")
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
            self._skipped_list.addItem(row)

        kept = len(result.data.get("layers", []))
        skip = len(result.skipped)
        self._summary_label.setText(
            f"{kept} layer(s) will publish; {skip} skipped. Viewport "
            f"center: "
            f"{snapshot.viewport.center_lng:.4f}, {snapshot.viewport.center_lat:.4f} "
            f"@ zoom {snapshot.viewport.zoom:.1f}."
        )
        # Publish stays enabled even when 0 layers map -- an empty
        # map is a legitimate starting point. The skipped warning
        # already tells the user what's missing.

    def _on_publish(self) -> None:
        profile_name = self._connection_combo.currentData()
        if not profile_name:
            QMessageBox.warning(
                self,
                "No connection selected",
                "Pick a signed-in connection to publish to.",
            )
            return
        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            QMessageBox.warning(self, "Connection not ready", "Sign in to the connection first.")
            return
        if self._translation is None:
            return

        # Refresh the title in case the user typed after the
        # initial snapshot.
        title = self._title_input.text().strip() or "Untitled map"
        description = self._description_input.toPlainText().strip() or None
        data = dict(self._translation.data)

        async def _create() -> ItemSummary:
            async with _connected_client(profile) as client:
                item = await client.items.create(
                    type="map",
                    title=title,
                    description=description,
                    data=data,
                )
                return item

        try:
            item = _run(_create())
        except Exception as e:  # pragma: no cover -- defensive
            _log.exception("publish-project failed")
            QMessageBox.critical(self, "Publish failed", str(e))
            return

        QMessageBox.information(
            self,
            "Published",
            f"Map '{item.title}' created on the portal.\n\n"
            f"Item id: {item.id}",
        )
        self.accept()


# -----------------------------------------------------------
# Helpers that touch QGIS state (kept out of project_to_map.py
# so the translation logic stays testable in isolation).
# -----------------------------------------------------------


def _build_snapshot(iface: QgisInterface, title: str) -> ProjectSnapshot:
    canvas = iface.mapCanvas()
    project = QgsProject.instance()
    layer_tree = project.layerTreeRoot()

    layers: list[CanvasLayer] = []
    # Walk the tree top-down so the resulting layer order matches
    # what the user sees in the Layers panel.
    for tree_layer in layer_tree.findLayers():
        ml = tree_layer.layer()
        if ml is None:
            continue
        provider = ml.providerType() if hasattr(ml, "providerType") else ""
        layers.append(
            CanvasLayer(
                name=ml.name(),
                source_uri=ml.source(),
                provider=str(provider),
                visible=bool(tree_layer.isVisible()),
                opacity=_layer_opacity(ml),
            )
        )

    # Reproject the canvas center to CRS84 (lon/lat) so the
    # publish payload always speaks the same CRS regardless of
    # the user's project CRS.
    center = canvas.center()
    src_crs = canvas.mapSettings().destinationCrs()
    crs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if src_crs.isValid() and src_crs != crs84:
        transform = QgsCoordinateTransform(src_crs, crs84, project)
        center = transform.transform(center)

    # Approximate zoom from scale. QGIS doesn't expose a tile-zoom
    # value directly; the conventional conversion is
    #   zoom = log2(559082264 / scale)
    # using the OGC WebMercatorQuad zoom-0 scale denominator.
    scale = canvas.scale()
    import math

    zoom = math.log2(559082264.0287178 / scale) if scale > 0 else 0.0

    return ProjectSnapshot(
        title=title,
        layers=layers,
        viewport=CanvasViewport(
            center_lng=center.x(),
            center_lat=center.y(),
            zoom=max(0.0, min(22.0, zoom)),
        ),
    )


def _layer_opacity(ml) -> float:
    """Best-effort opacity extraction across raster + vector layers."""
    if hasattr(ml, "opacity"):
        try:
            return float(ml.opacity())
        except Exception:
            return 1.0
    if hasattr(ml, "renderer") and hasattr(ml.renderer(), "opacity"):
        try:
            return float(ml.renderer().opacity())
        except Exception:
            return 1.0
    return 1.0


def _project_title_or_fallback() -> str:
    title = QgsProject.instance().title()
    if title:
        return title
    return "QGIS map"
