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

from typing import TYPE_CHECKING, Any

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

from ..log import get_logger
from ..offline.clone import read_clone_source
from ..portal import get_client
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
from ..publish.source import PUBLISHED_ITEM_PROPERTY, PUBLISHED_LAYER_PROPERTY
from ..settings import ConnectionProfile, ConnectionStore
from ..tasks import format_error, run_in_task

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
        # Portal item index for backref matching. Starts empty so the
        # dialog can render the layer audit instantly; the real index
        # loads in a background task and re-renders when it lands.
        self._index = PortalIndex()
        # QGIS layer ids the user unticked. Held here rather than read
        # off the widgets at publish time so the choice survives the
        # list being rebuilt, which happens on every index refresh.
        self._excluded_layer_ids: set[str] = set()
        self._busy = False
        # Set on reject: task completions landing after the user
        # dismissed the dialog must not pop message boxes or keep
        # refreshing a dead widget tree.
        self._closed = False

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
        layout.addWidget(QLabel("Layers to include in the map (untick to leave out):"))
        layout.addWidget(self._included_list, 2)
        layout.addWidget(
            QLabel("Layers not on the portal (won't be in the map):")
        )
        layout.addWidget(self._skipped_list, 1)
        layout.addWidget(self._bulk_add_button)
        layout.addWidget(self._summary_label)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self._included_list.itemChanged.connect(self._on_layer_ticked)

        # Populate the lists with the current project state, then
        # load the portal index in the background. Publishing stays
        # locked until the index resolves so a fast Publish click
        # cannot race the backref matching and ship a map that skips
        # layers the index would have matched.
        self._snapshot_and_render()
        self._connection_combo.currentIndexChanged.connect(self._refresh_index)
        self._refresh_index()

    # ----- Internals -----

    def _refresh_index(self) -> None:
        """Load the selected portal's basemap + service index in a
        task so ``translate`` can backref external-service layers
        (ArcGIS REST, XYZ basemaps) to the portal items they came
        from. Empty index when no connection is signed in or the
        fetch errors; the translator then skips those layers
        without crashing, same as before the index existed.
        """
        profile_name = self._connection_combo.currentData()
        profile = self._store.get(profile_name) if profile_name else None
        if profile is None or not profile.is_discovered:
            self._index = PortalIndex()
            self._snapshot_and_render()
            return

        self._set_busy(True)
        self._summary_label.setText("Checking which layers are on the portal...")

        def fetch(_handle) -> PortalIndex:
            return _fetch_portal_index(profile)

        def done(index: PortalIndex) -> None:
            if self._closed:
                return
            self._index = index
            self._set_busy(False)
            self._snapshot_and_render()

        def failed(exc: BaseException) -> None:
            # Best-effort: publish still works, just without portal
            # backrefs for external services and basemaps.
            _log.error("portal index fetch failed; publish without backrefs", exc_info=exc)
            if self._closed:
                return
            self._index = PortalIndex()
            self._set_busy(False)
            self._snapshot_and_render()

        run_in_task("GratisGIS portal index", fetch, done, failed, cancelable=False)

    def reject(self) -> None:  # Qt override
        self._closed = True
        super().reject()

    def _set_busy(self, busy: bool) -> None:
        """One switch for every surface that must not re-enter while
        a task (index load, item create, map publish) is running."""
        self._busy = busy
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not busy)
        self._bulk_add_button.setEnabled(
            not busy and self._bulk_add_button.isVisible()
        )
        self._connection_combo.setEnabled(not busy)

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
        """Rebuild the layer audit from the project as it stands now.

        Deliberately renders the WHOLE project, including layers the
        user has unticked: an excluded layer that disappeared from the
        list could never be put back. What is ticked is applied when
        publishing instead, by ``_publish_snapshot``.
        """
        snapshot = _build_snapshot(self._iface, title=self._title_input.text())
        result = translate(snapshot, portal_index=self._index)
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
        # One checkbox per layer. Ticked means "put this in the map";
        # everything on the portal starts ticked, because a layer being
        # in your project is the reason you opened this dialog.
        # zip rather than counting rows: the basemap row above has no
        # layer of its own, so any index arithmetic here would be off
        # by one exactly when a basemap is present.
        layers = result.data.get("layers", [])
        ids = list(result.included_layer_ids) + [""] * len(layers)
        for lyr, layer_id in zip(layers, ids, strict=False):
            row = QListWidgetItem(
                f"{lyr['title']}  ->  {_format_source(lyr['source'])}"
            )
            row.setFlags(
                (row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsSelectable
            )
            row.setData(Qt.ItemDataRole.UserRole, layer_id)
            row.setCheckState(
                Qt.CheckState.Unchecked
                if layer_id and layer_id in self._excluded_layer_ids
                else Qt.CheckState.Checked
            )
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
        self._bulk_add_button.setEnabled(bulk_ready and not self._busy)
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

    def _on_layer_ticked(self, item: QListWidgetItem) -> None:
        """Remember an untick so it survives the list being rebuilt.

        The list is rebuilt on every index refresh, which would
        otherwise silently re-tick everything the user had turned off.
        """
        layer_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(layer_id, str) or not layer_id:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._excluded_layer_ids.discard(layer_id)
        else:
            self._excluded_layer_ids.add(layer_id)
        self._refresh_summary()

    def _publish_translation(self) -> MapTranslation:
        """The translation to publish, honouring the checkboxes.

        Re-translated from a snapshot with the unticked layers removed
        rather than by deleting entries from the rendered result: a
        layer can affect more than its own row (a basemap sets a field
        instead of adding a layer), so filtering the output would leave
        the payload disagreeing with the ticks.
        """
        snapshot = _build_snapshot(self._iface, title=self._title_input.text())
        kept = ProjectSnapshot(
            title=snapshot.title,
            layers=[
                lyr
                for lyr in snapshot.layers
                if (lyr.qgis_layer_id or "") not in self._excluded_layer_ids
            ],
            viewport=snapshot.viewport,
        )
        return translate(kept, portal_index=self._index)

    def _refresh_summary(self) -> None:
        """Recount what will be published, without rebuilding the list."""
        result = self._publish_translation()
        kept = len(result.data.get("layers", []))
        basemap_note = " (plus 1 basemap)" if result.data.get("basemap") else ""
        left_out = len(self._excluded_layer_ids)
        excluded_note = f", {left_out} unticked" if left_out else ""
        self._summary_label.setText(
            f"{kept} layer(s){basemap_note} will be referenced from the new "
            f"map{excluded_note}."
        )

    # ----- Actions on skipped rows -----

    def _on_skipped_action(self, skipped: SkippedLayer) -> None:
        """Dispatch to the right "add to portal" flow per layer
        kind. Each flow refreshes the dialog when its add lands so
        the row moves up to the included list.
        """
        if self._busy:
            return
        profile = self._current_profile()
        if profile is None:
            QMessageBox.warning(
                self,
                "No connection selected",
                "Pick a signed-in connection to publish to.",
            )
            return
        if skipped.service_url is not None and skipped.service_type is not None:
            self._run_create_service(profile, skipped)
        elif skipped.basemap_tile_url is not None:
            self._run_create_basemap(profile, skipped)
        elif skipped.is_local_vector and self._run_publish_vector(skipped):
            self._refresh_index()

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

        self._set_busy(True)
        self._summary_label.setText(f"Creating {len(candidates)} portal item(s)...")

        def create_all(_handle) -> tuple[int, list[tuple[str, str]]]:
            created = 0
            failures: list[tuple[str, str]] = []
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
                except Exception as e:  # pragma: no cover - defensive
                    _log.exception("bulk add-missing failed for %s", s.name)
                    failures.append((s.name, str(e)))
            return created, failures

        def done(result: tuple[int, list[tuple[str, str]]]) -> None:
            created, failures = result
            if self._closed:
                return
            self._set_busy(False)
            if failures:
                QMessageBox.warning(
                    self,
                    "Some items failed",
                    f"Created {created}, failed {len(failures)}.\n\n"
                    + "\n".join(f"- {n}: {err}" for n, err in failures),
                )
            else:
                QMessageBox.information(
                    self,
                    "Items created",
                    f"{created} item(s) created on the portal.",
                )
            self._refresh_index()

        def failed(exc: BaseException) -> None:  # pragma: no cover - defensive
            _log.error("bulk add-missing failed", exc_info=exc)
            if self._closed:
                return
            self._set_busy(False)
            QMessageBox.critical(self, "Create failed", format_error(exc))

        run_in_task("GratisGIS: add portal items", create_all, done, failed, cancelable=False)

    def _run_create_service(
        self, profile: ConnectionProfile, skipped: SkippedLayer
    ) -> None:
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
            return
        service_url = skipped.service_url
        service_type = skipped.service_type
        layer_id = skipped.service_layer_id
        title = dlg.title()
        description = dlg.description()
        access = dlg.access()

        def create(_handle) -> ItemSummary:
            return _create_service_item(
                profile,
                title=title,
                url=service_url,
                service_type=service_type,
                layer_id=layer_id,
                description=description,
                access=access,
            )

        self._run_create_task("GratisGIS: add connected service", create)

    def _run_create_basemap(
        self, profile: ConnectionProfile, skipped: SkippedLayer
    ) -> None:
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
            return
        tile_url = skipped.basemap_tile_url
        title = dlg.title()
        description = dlg.description()
        access = dlg.access()

        def create(_handle) -> ItemSummary:
            return _create_basemap_item(
                profile,
                title=title,
                tile_url=tile_url,
                description=description,
                access=access,
            )

        self._run_create_task("GratisGIS: add basemap", create)

    def _run_create_task(self, description: str, create) -> None:
        """Shared scheduling for the one-item create actions."""
        self._set_busy(True)

        def done(_item: ItemSummary) -> None:
            if self._closed:
                return
            self._set_busy(False)
            # Re-index so the fresh item backrefs and the row moves
            # up into the included list.
            self._refresh_index()

        def failed(exc: BaseException) -> None:
            _log.error("create item failed", exc_info=exc)
            if self._closed:
                return
            self._set_busy(False)
            QMessageBox.critical(self, "Create failed", format_error(exc))

        run_in_task(description, create, done, failed, cancelable=False)

    def _run_publish_vector(self, skipped: SkippedLayer) -> bool:
        """Launch the existing Phase 3 publish-vector-layer dialog
        with the canvas layer preselected.
        """
        # Lazy import keeps the vector dialog out of this module's
        # top-level imports so an error in the vector dialog
        # doesn't crash project publish.
        from .publish_vector_dialog import PublishLayerDialog

        dlg = PublishLayerDialog(
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
        # Re-translated at the moment of publishing, so what ships is
        # what the ticks say now rather than what the list said when it
        # was last drawn.
        data = dict(self._publish_translation().data)

        self._set_busy(True)
        self._summary_label.setText("Creating map item on the portal...")

        def create(_handle) -> ItemSummary:
            return get_client(profile).items.create(
                type="map",
                title=title,
                description=description,
                data=data,
            )

        def done(item: ItemSummary) -> None:
            if self._closed:
                _log.info("map %s created after the dialog closed", item.id)
                return
            self._set_busy(False)
            QMessageBox.information(
                self,
                "Published",
                f"Map '{item.title}' created on the portal.\n\n"
                f"Item id: {item.id}",
            )
            self.accept()

        def failed(exc: BaseException) -> None:
            _log.error("publish-project failed", exc_info=exc)
            if self._closed:
                return
            self._set_busy(False)
            self._snapshot_and_render()
            QMessageBox.critical(self, "Publish failed", format_error(exc))

        run_in_task("GratisGIS: publish map", create, done, failed, cancelable=False)


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
    index them by their upstream URLs. Blocking; run it in a task.

    Basemap items expose ``data.tileUrl``, the literal XYZ
    template the plugin's BasemapItem encodes into the WMS XYZ
    layer URI when the user drags a basemap onto the canvas.
    Service items expose ``data.url``, the MapServer or
    FeatureServer root.

    Both lookups are then used by ``translate`` to backref a
    QGIS layer (whose source URL points at the EXTERNAL service)
    to the portal item the layer originally came from.
    """
    basemaps: dict[str, str] = {}
    services: dict[str, PortalServiceRef] = {}
    client = get_client(profile)
    # Pull all items in scope: typical org has <500 connected
    # items so a single page is fine.
    items = client.items.list(limit=1000)
    for it in items.items:
        # ItemSummary doesn't carry `data`, so fetch full only for
        # the types we actually index. Skipping data_layer / file /
        # map etc. keeps this cheap.
        normalized = (it.type or "").replace("-", "_")
        if normalized not in (
            "basemap",
            "service",
            "arcgis_service",
        ):
            continue
        full = client.items.get(it.id)
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
    # Raster layers already on the canvas need one extra lookup each,
    # so gather their ids from the project rather than walking every
    # tile layer on the portal.
    tile_ids = _tile_layer_ids_on_canvas()
    return PortalIndex(
        basemaps_by_tile_url=basemaps,
        services_by_url=services,
        tile_layers_by_item=_tile_layer_index(client, tile_ids),
    )


def _tile_layer_ids_on_canvas() -> set[str]:
    """Portal raster item ids among the project's layers."""
    from ..browser.uris import parse_tile_layer_uri

    found: set[str] = set()
    for layer in QgsProject.instance().mapLayers().values():
        try:
            source = layer.source()
        except Exception:  # pragma: no cover - defensive
            continue
        if not isinstance(source, str):
            continue
        parsed = parse_tile_layer_uri(source)
        if parsed is not None:
            found.add(parsed[1])
    return found


def _tile_layer_index(client: Any, item_ids: set[str]) -> dict[str, Any]:
    """Fetch what a map needs for each recognised raster.

    A tile layer's map source has to carry the item's own tile URL,
    which lives in the item's data envelope and nowhere in the QGIS
    layer, so recognising the item is not enough on its own.

    One request per raster, and only for rasters actually on the
    canvas. A failure is swallowed per item: the layer then reports
    that its details could not be read, which is better than failing
    the whole dialog over one item.
    """
    from ..publish.project_to_map import PortalTileLayerRef

    out: dict[str, Any] = {}
    for item_id in item_ids:
        try:
            item = client.items.get(item_id)
        except Exception:
            _log.debug("could not read tile layer %s", item_id, exc_info=True)
            continue
        data = item.data if isinstance(item.data, dict) else {}
        tile_url = data.get("tileUrl")
        if not isinstance(tile_url, str) or not tile_url:
            continue
        out[item_id] = PortalTileLayerRef(
            tile_url=tile_url, bbox_wgs84=item.bbox
        )
    return out


def _known_portal_origin(layer: Any) -> tuple[str | None, str | None]:
    """The portal item a layer stands for, when its URI cannot say.

    Two cases, both of which look like an ordinary local file and were
    therefore offered for publishing despite already being on the
    portal:

      - an **offline clone**, which records where it came from inside
        the GeoPackage. Its data IS a portal layer, so a map should
        point at the source rather than treat the copy as new. Read
        from the file, so it works even in a fresh QGIS session that
        has never seen the clone before.
      - a layer **this plugin published**, which stamps the resulting
        item onto the QGIS layer. Publishing a layer and then
        publishing the project used to offer to publish it twice.

    Both are best-effort: this runs over every layer in the project,
    most of which have nothing to do with the portal.
    """
    stamped = _published_item_property(layer)
    if stamped[0]:
        return stamped

    try:
        source = layer.source()
    except Exception:  # pragma: no cover - defensive
        return (None, None)
    if not isinstance(source, str):
        return (None, None)
    path = source.split("|", 1)[0].strip()
    if not path.lower().endswith(".gpkg"):
        return (None, None)
    ref = read_clone_source(path)
    if ref is None:
        return (None, None)
    # "default" is the portal's alias for a single-layer item; a map
    # source names no sublayer in that case.
    layer_key = ref.layer_id if ref.layer_id != "default" else None
    return (ref.item_id, layer_key)


def _published_item_property(layer: Any) -> tuple[str | None, str | None]:
    """Read the portal item this plugin stamped onto the layer."""
    try:
        item_id = layer.customProperty(PUBLISHED_ITEM_PROPERTY)
        layer_key = layer.customProperty(PUBLISHED_LAYER_PROPERTY)
    except Exception:  # pragma: no cover - defensive
        return (None, None)
    if not isinstance(item_id, str) or not item_id.strip():
        return (None, None)
    key = layer_key.strip() if isinstance(layer_key, str) else ""
    return (item_id.strip(), key or None)


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
        item_id, layer_key = _known_portal_origin(ml)
        layers.append(
            CanvasLayer(
                name=ml.name(),
                source_uri=ml.source(),
                provider=str(provider),
                visible=bool(tree_layer.isVisible()),
                opacity=_layer_opacity(ml),
                qgis_layer_id=ml.id() if hasattr(ml, "id") else None,
                portal_item_id=item_id,
                portal_layer_key=layer_key,
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

    Blocking; run it in a task.

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
    return get_client(profile).items.create(
        type="service",
        title=title,
        data=data,
        description=description,
        access=access,  # type: ignore[arg-type]
    )


def _create_basemap_item(
    profile: ConnectionProfile,
    *,
    title: str,
    tile_url: str,
    description: str | None = None,
    access: str = "private",
) -> ItemSummary:
    """Create a portal ``basemap`` item from a tile URL template.

    Blocking; run it in a task.
    """
    data: dict[str, object] = {
        "kind": "tile-url",
        "tileUrl": tile_url,
        "version": 1,
    }
    return get_client(profile).items.create(
        type="basemap",
        title=title,
        data=data,
        description=description,
        access=access,  # type: ignore[arg-type]
    )
