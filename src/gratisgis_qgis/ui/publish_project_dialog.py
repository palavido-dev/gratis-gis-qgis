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
from qgis.PyQt.QtCore import QSize, Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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
    PortalIndex,
    PortalServiceRef,
    ProjectSnapshot,
    SkippedLayer,
    translate,
)
from ..settings import ConnectionProfile, ConnectionStore

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
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Publish")
        buttons.accepted.connect(self._on_publish)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons

        # ----- Compose layout -----
        # Wording note: only the map item itself is new on the
        # portal; each layer below is already a portal item that
        # the new map will reference. "Layers included in the
        # map" / "Layers not on the portal (won't be in the map)"
        # keeps that distinction explicit so users don't expect
        # the publish to upload anything but the map.
        self._bulk_add_button = QPushButton(
            "Add all missing services + basemaps to portal"
        )
        self._bulk_add_button.clicked.connect(self._on_bulk_add_missing)
        self._bulk_add_button.setVisible(False)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(QLabel("Layers included in the map:"))
        layout.addWidget(self._included_list, 2)
        layout.addWidget(
            QLabel("Layers not on the portal (won't be in the map):")
        )
        layout.addWidget(self._skipped_list, 1)
        layout.addWidget(self._bulk_add_button)
        layout.addWidget(self._summary_label)
        layout.addWidget(buttons)
        self.setLayout(layout)

        # Populate the lists with the current project state.
        self._snapshot_and_render()

    # ----- Internals -----

    def _build_portal_index(self) -> PortalIndex:
        """Pre-fetch the currently-selected portal's basemap + service
        items and build URL lookups so ``translate`` can backref
        external-service layers (ArcGIS REST, XYZ basemaps) to the
        portal items they came from. Empty index when no
        connection is signed in or the fetch errors -- the
        translator falls back to skipping those layers without
        crashing.
        """
        profile_name = self._connection_combo.currentData()
        if not profile_name:
            return PortalIndex()
        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            return PortalIndex()
        try:
            return _fetch_portal_index(profile)
        except Exception:  # pragma: no cover -- best-effort
            _log.exception("portal index fetch failed; publish without backrefs")
            return PortalIndex()

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
        index = self._build_portal_index()
        result = translate(snapshot, portal_index=index)
        self._translation = result

        self._included_list.clear()
        # A matched basemap doesn't appear in the layers list (it
        # sets MapData.basemap instead), so surface it explicitly
        # at the top of the included list -- otherwise the user
        # sees their basemap in the QGIS canvas, sees an "OK"
        # publish, but can't find it in the dialog summary.
        if result.data.get("basemap"):
            row = QListWidgetItem(
                f"Basemap  ->  basemap item ({result.data['basemap']})"
            )
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._included_list.addItem(row)
        for lyr in result.data.get("layers", []):
            row = QListWidgetItem(
                f"{lyr['title']}  ->  {_format_source(lyr['source'])}"
            )
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._included_list.addItem(row)

        self._skipped_list.clear()
        skipped_with_actions = 0
        for s in result.skipped:
            item = QListWidgetItem()
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            widget = _SkippedRowWidget(s, on_action=self._on_skipped_action)
            item.setSizeHint(widget.sizeHint())
            self._skipped_list.addItem(item)
            self._skipped_list.setItemWidget(item, widget)
            if _has_action(s):
                skipped_with_actions += 1

        # Bulk-add button is enabled only when there are metadata-
        # only skipped layers (services + basemaps) we can create
        # in one pass. Local-file rows need the vector-publish
        # dialog and run one-at-a-time.
        bulk_ready = any(
            s.service_url or s.basemap_tile_url for s in result.skipped
        )
        self._bulk_add_button.setEnabled(bulk_ready)
        self._bulk_add_button.setVisible(skipped_with_actions > 0)

        kept = len(result.data.get("layers", []))
        skip = len(result.skipped)
        basemap_note = ""
        if result.data.get("basemap"):
            basemap_note = " (plus 1 basemap)"
        self._summary_label.setText(
            f"{kept} layer(s){basemap_note} will be referenced from the new "
            f"map; {skip} skipped. Viewport center: "
            f"{snapshot.viewport.center_lng:.4f}, {snapshot.viewport.center_lat:.4f} "
            f"@ zoom {snapshot.viewport.zoom:.1f}."
        )
        # Publish stays enabled even when 0 layers map -- an empty
        # map is a legitimate starting point. The skipped list
        # already tells the user which canvas layers won't carry
        # over.

    # ----- Actions on skipped rows -----

    def _on_skipped_action(self, skipped: SkippedLayer) -> None:
        """Dispatch to the right "add to portal" flow per layer
        kind. Refresh the dialog snapshot after a successful add
        so the row moves up to the included list.
        """
        profile = self._current_profile()
        if profile is None:
            QMessageBox.warning(
                self,
                "No connection selected",
                "Pick a signed-in connection to publish to.",
            )
            return
        ok = False
        if skipped.service_url is not None and skipped.service_type is not None:
            ok = self._run_create_service(profile, skipped)
        elif skipped.basemap_tile_url is not None:
            ok = self._run_create_basemap(profile, skipped)
        elif skipped.is_local_vector:
            ok = self._run_publish_vector(skipped)
        if ok:
            self._snapshot_and_render()

    def _on_bulk_add_missing(self) -> None:
        """Quick-add every metadata-only skipped layer (services +
        basemaps) in one pass. Local-file rows skipped -- those
        need the per-layer vector-publish dialog.
        """
        if self._translation is None:
            return
        profile = self._current_profile()
        if profile is None:
            QMessageBox.warning(
                self,
                "No connection selected",
                "Pick a signed-in connection to publish to.",
            )
            return
        candidates = [
            s
            for s in self._translation.skipped
            if s.service_url is not None or s.basemap_tile_url is not None
        ]
        if not candidates:
            return

        created = 0
        failed: list[tuple[str, str]] = []
        for s in candidates:
            try:
                if s.service_url and s.service_type:
                    _create_service_item(
                        profile,
                        title=s.name,
                        url=s.service_url,
                        service_type=s.service_type,
                        layer_id=s.service_layer_id,
                    )
                elif s.basemap_tile_url:
                    _create_basemap_item(
                        profile, title=s.name, tile_url=s.basemap_tile_url
                    )
                created += 1
            except Exception as e:  # pragma: no cover -- defensive
                _log.exception("bulk add-missing failed for %s", s.name)
                failed.append((s.name, str(e)))

        if failed:
            QMessageBox.warning(
                self,
                "Some items failed",
                f"Created {created}, failed {len(failed)}.\n\n"
                + "\n".join(f"- {n}: {err}" for n, err in failed),
            )
        else:
            QMessageBox.information(
                self,
                "Items created",
                f"{created} item(s) created on the portal.",
            )
        self._snapshot_and_render()

    def _run_create_service(
        self, profile: ConnectionProfile, skipped: SkippedLayer
    ) -> bool:
        assert skipped.service_url is not None
        assert skipped.service_type is not None
        dlg = _QuickItemDialog(
            self,
            window_title="Add connected service to portal",
            label=(
                f"Create a new connected-service item for:\n"
                f"  {skipped.service_url}"
            ),
            default_title=skipped.name,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        try:
            _create_service_item(
                profile,
                title=dlg.title(),
                url=skipped.service_url,
                service_type=skipped.service_type,
                layer_id=skipped.service_layer_id,
                description=dlg.description(),
                access=dlg.access(),
            )
        except Exception as e:  # pragma: no cover -- defensive
            _log.exception("create service failed")
            QMessageBox.critical(self, "Create failed", str(e))
            return False
        return True

    def _run_create_basemap(
        self, profile: ConnectionProfile, skipped: SkippedLayer
    ) -> bool:
        assert skipped.basemap_tile_url is not None
        dlg = _QuickItemDialog(
            self,
            window_title="Add basemap to portal",
            label=(
                f"Create a new basemap item for:\n"
                f"  {skipped.basemap_tile_url}"
            ),
            default_title=skipped.name,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        try:
            _create_basemap_item(
                profile,
                title=dlg.title(),
                tile_url=skipped.basemap_tile_url,
                description=dlg.description(),
                access=dlg.access(),
            )
        except Exception as e:  # pragma: no cover -- defensive
            _log.exception("create basemap failed")
            QMessageBox.critical(self, "Create failed", str(e))
            return False
        return True

    def _run_publish_vector(self, skipped: SkippedLayer) -> bool:
        """Launch the existing Phase 3 publish-vector-layer dialog
        with the canvas layer preselected.
        """
        # Lazy import keeps the vector dialog out of this module's
        # top-level imports so an error in the vector dialog
        # doesn't crash project publish.
        from .publish_vector_dialog import PublishVectorDialog

        dlg = PublishVectorDialog(
            self._iface, parent=self, preselect_layer_id=skipped.local_layer_id
        )
        dlg.exec()
        # The user may or may not have actually published; either
        # way refresh so a successful add gets picked up. The
        # vector dialog doesn't return a status value we can
        # introspect.
        return True

    def _current_profile(self) -> ConnectionProfile | None:
        profile_name = self._connection_combo.currentData()
        if not profile_name:
            return None
        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            return None
        return profile

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


def _format_source(source: dict[str, object]) -> str:
    """One-line summary of a MapLayerSource for the included
    list. Each branch labels the portal-item id the new map will
    reference so the user can sanity-check the mapping.
    """
    kind = str(source.get("kind", "?"))
    if kind == "data-layer":
        item_id = str(source.get("itemId", "?"))
        layer_key = source.get("layerKey")
        if isinstance(layer_key, str) and layer_key:
            return f"data layer ({item_id} / {layer_key})"
        return f"data layer ({item_id})"
    if kind == "arcgis-rest":
        url = str(source.get("url", "?"))
        layer_id = source.get("layerId", "?")
        item_id = source.get("sourceItemId")
        service_type = str(source.get("serviceType", "ArcGIS"))
        if isinstance(item_id, str) and item_id:
            return f"{service_type} layer {layer_id} (portal item {item_id})"
        # No portal-item backref -- show the URL so the user
        # can spot off-portal references that will work but
        # aren't tracked through portal admin.
        return f"{service_type} layer {layer_id} ({url}) - not a portal item"
    return kind


def _fetch_portal_index(profile: ConnectionProfile) -> PortalIndex:
    """Pull the portal's basemap + connected-service items and
    index them by their upstream URLs.

    Basemap items expose ``data.tileUrl`` -- the literal XYZ
    template the plugin's BasemapItem encodes into the WMS XYZ
    layer URI when the user drags a basemap onto the canvas.
    Service items expose ``data.url`` -- the MapServer or
    FeatureServer root.

    Both lookups are then used by ``translate`` to backref a
    QGIS layer (whose source URL points at the EXTERNAL service)
    to the portal item the layer originally came from.
    """
    from ..browser.fetch import _connected_client

    async def _do() -> tuple[
        dict[str, str], dict[str, PortalServiceRef]
    ]:
        basemaps: dict[str, str] = {}
        services: dict[str, PortalServiceRef] = {}
        async with _connected_client(profile) as client:
            # Pull all items in scope -- typical org has <500
            # connected items so a single page is fine.
            items = await client.items.list(limit=1000)
            for it in items.items:
                # ItemSummary doesn't carry `data`, so fetch full
                # only for the types we actually index. Skipping
                # data_layer / file / map etc. keeps this cheap.
                normalized = (it.type or "").replace("-", "_")
                if normalized not in (
                    "basemap",
                    "service",
                    "arcgis_service",
                ):
                    continue
                full = await client.items.get(it.id)
                data = full.data if hasattr(full, "data") else None
                if not isinstance(data, dict):
                    continue
                if normalized == "basemap":
                    tile_url = data.get("tileUrl")
                    if isinstance(tile_url, str) and tile_url:
                        basemaps[tile_url] = it.id
                else:
                    url = data.get("url")
                    if not isinstance(url, str) or not url:
                        continue
                    root = url.rstrip("/")
                    if "/FeatureServer" in root:
                        services[root] = PortalServiceRef(
                            item_id=it.id, service_type="FeatureServer"
                        )
                    elif "/MapServer" in root:
                        services[root] = PortalServiceRef(
                            item_id=it.id, service_type="MapServer"
                        )
        return basemaps, services

    basemaps, services = _run(_do())
    return PortalIndex(basemaps_by_tile_url=basemaps, services_by_url=services)


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
                qgis_layer_id=ml.id() if hasattr(ml, "id") else None,
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


def _has_action(s: SkippedLayer) -> bool:
    return bool(s.service_url or s.basemap_tile_url or s.is_local_vector)


class _SkippedRowWidget(QWidget):
    """Custom row widget for the skipped-layers list. Renders the
    layer's name + skip reason on the left and a contextual
    "Add to portal..." button on the right (when one fits).
    """

    def __init__(
        self,
        skipped: SkippedLayer,
        *,
        on_action,
    ) -> None:
        super().__init__()
        self._skipped = skipped
        self._on_action = on_action

        text = f"{skipped.name}  [{skipped.provider}]\n{skipped.reason}"
        label = QLabel(text)
        label.setWordWrap(True)

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(label, 1)

        button_label = self._button_label_for(skipped)
        if button_label is not None:
            btn = QPushButton(button_label)
            btn.setMinimumSize(QSize(180, 26))
            btn.clicked.connect(self._invoke_action)
            layout.addWidget(btn, 0)

        self.setLayout(layout)

    @staticmethod
    def _button_label_for(skipped: SkippedLayer) -> str | None:
        if skipped.service_url is not None:
            return "Add as connected service..."
        if skipped.basemap_tile_url is not None:
            return "Add as basemap..."
        if skipped.is_local_vector:
            return "Publish as data layer..."
        return None

    def _invoke_action(self) -> None:
        self._on_action(self._skipped)


class _QuickItemDialog(QDialog):
    """Tiny one-screen dialog for creating a metadata-only portal
    item (service / basemap) from values the publish flow
    already has.

    The URL is already known from the canvas layer URI, so the
    dialog only asks for the title + sharing scope. Description
    is optional. The window's caller composes the actual create
    request from the returned values.
    """

    def __init__(
        self,
        parent: QWidget,
        *,
        window_title: str,
        label: str,
        default_title: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.setMinimumWidth(440)

        self._title_input = QLineEdit()
        self._title_input.setText(default_title)
        self._description_input = QPlainTextEdit()
        self._description_input.setFixedHeight(56)
        self._access_combo = QComboBox()
        self._access_combo.addItem("Private (only you)", "private")
        self._access_combo.addItem("Org (everyone in your org)", "org")
        self._access_combo.addItem("Public (anyone with the link)", "public")

        form = QFormLayout()
        form.addRow("Title:", self._title_input)
        form.addRow("Description:", self._description_input)
        form.addRow("Access:", self._access_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add to portal")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        info_label = QLabel(label)
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def title(self) -> str:
        return self._title_input.text().strip() or "Untitled"

    def description(self) -> str | None:
        text = self._description_input.toPlainText().strip()
        return text or None

    def access(self) -> str:
        return self._access_combo.currentData() or "private"


def _create_service_item(
    profile: ConnectionProfile,
    *,
    title: str,
    url: str,
    service_type: str,
    layer_id: int | None = None,
    description: str | None = None,
    access: str = "private",
) -> ItemSummary:
    """Create a portal ``service`` item from an ArcGIS REST URL.

    Mirrors the data envelope the portal seeds for connected
    services: ``url`` is the service root, ``layers`` carries at
    least the selected layer index so the auto-create item is
    immediately usable in maps. The portal admin can re-probe to
    fill in the rest of the service's layer metadata.
    """
    layers_list: list[dict[str, object]] = []
    if layer_id is not None:
        layers_list.append(
            {
                "name": str(layer_id),
                "title": title,
            }
        )
    data: dict[str, object] = {
        "url": url,
        "layers": layers_list,
        "version": 1,
        "protocol": (
            "arcgis_map" if service_type == "MapServer" else "arcgis_features"
        ),
        "serviceTitle": title,
        "selectedLayerIds": [layer_id] if layer_id is not None else [],
    }

    async def _do() -> ItemSummary:
        async with _connected_client(profile) as client:
            return await client.items.create(
                type="service",
                title=title,
                data=data,
                description=description,
                access=access,  # type: ignore[arg-type]
            )

    return _run(_do())


def _create_basemap_item(
    profile: ConnectionProfile,
    *,
    title: str,
    tile_url: str,
    description: str | None = None,
    access: str = "private",
) -> ItemSummary:
    """Create a portal ``basemap`` item from a tile URL template."""
    data: dict[str, object] = {
        "kind": "tile-url",
        "tileUrl": tile_url,
        "version": 1,
    }

    async def _do() -> ItemSummary:
        async with _connected_client(profile) as client:
            return await client.items.create(
                type="basemap",
                title=title,
                data=data,
                description=description,
                access=access,  # type: ignore[arg-type]
            )

    return _run(_do())
