# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search dock widget (Phase 2).

The Browser tree (Phase 1) is great for "I know what folder it's
in" navigation. The Search dock is for "find me everything tagged
parcels in the org's Maps." Two filters in v1:

  - free-text query against title + description + tags (`?q=`)
  - item-type filter (data_layer, map, web_app, ...)

Results render as a list with double-click = "add to QGIS canvas"
(routes through the same OAPIF URI shape Phase 1 emits) and
right-click = "show item properties" (opens the ItemPropertiesDialog
from Phase 2's other widget).

The dock keeps a single live connection profile pick at the top so
multi-portal users can swap between portals without re-typing the
query. The picker reads the same ConnectionStore the Browser tree
uses, so adding/removing a profile updates both surfaces.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gratisgis_client.models.item import ItemSummary, ItemType

from ..browser.buckets import is_qgis_consumable
from ..browser.uris import oapif_uri, vector_tile_uri
from ..log import get_logger
from ..portal import list_items
from ..settings import ConnectionStore
from ..tasks import format_error, run_in_task

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)


# The user-facing type filter. None means "any (QGIS-consumable)
# type". Restricted to types we actually display -- the search
# dock filters out non-consumable items from results anyway (see
# is_qgis_consumable below), so offering a filter that returns 0
# results would be a footgun.
_TYPE_FILTERS: list[tuple[str, ItemType | None]] = [
    ("Any type", None),
    ("Data layer", "data_layer"),
    ("Derived layer", "derived_layer"),
    ("Tile layer", "tile_layer"),
    ("Basemap", "basemap"),
    ("Connected service", "service"),
    ("ArcGIS service", "arcgis_service"),
    ("WMS service", "wms_service"),
    ("WFS service", "wfs_service"),
]


class GratisGISSearchDock(QDockWidget):
    """Right-side dock with a search bar + results list."""

    OBJECT_NAME = "gratisgis_search_dock"

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__("GratisGIS search", parent)
        self.setObjectName(self.OBJECT_NAME)
        self._iface = iface
        self._store = ConnectionStore()
        # Monotonic search id; results for anything but the newest
        # search are dropped so a slow earlier query cannot overwrite
        # a faster later one.
        self._search_seq = 0

        # ----- Toolbar row -----
        self._connection_combo = QComboBox()
        self._reload_connections()

        self._type_combo = QComboBox()
        for label, _ in _TYPE_FILTERS:
            self._type_combo.addItem(label)

        self._query_input = QLineEdit()
        self._query_input.setPlaceholderText("Search by title, description, or tag")
        self._query_input.returnPressed.connect(self._on_search)

        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._on_search)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Portal:"))
        top_row.addWidget(self._connection_combo, 2)
        top_row.addWidget(QLabel("Type:"))
        top_row.addWidget(self._type_combo, 1)

        mid_row = QHBoxLayout()
        mid_row.addWidget(self._query_input, 5)
        mid_row.addWidget(self._search_btn)

        # ----- Results list -----
        self._results = QListWidget()
        self._results.itemDoubleClicked.connect(self._on_double_click)
        self._results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._results.customContextMenuRequested.connect(
            self._on_results_context_menu
        )

        self._status = QLabel("")
        self._status.setStyleSheet("color: #888;")

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(mid_row)
        layout.addWidget(self._results, 1)
        layout.addWidget(self._status)

        container = QWidget()
        container.setLayout(layout)
        self.setWidget(container)

    # ----- Public surface -----

    def refresh_connections(self) -> None:
        """Re-read the connection store and rebuild the picker.

        Called by the plugin shell when the connection manager
        dialog closes so a freshly added profile shows up here
        without restarting the plugin.
        """
        self._reload_connections()

    # ----- Internals -----

    def _reload_connections(self) -> None:
        self._connection_combo.blockSignals(True)
        self._connection_combo.clear()
        names = self._store.list_names()
        if not names:
            self._connection_combo.addItem("(no connections)", userData=None)
            self._connection_combo.setEnabled(False)
        else:
            for name in names:
                profile = self._store.get(name)
                if profile is None:
                    continue
                self._connection_combo.addItem(profile.display_label, userData=name)
            self._connection_combo.setEnabled(True)
        self._connection_combo.blockSignals(False)

    def _selected_profile_name(self) -> str | None:
        return self._connection_combo.currentData()

    def _selected_type(self) -> ItemType | None:
        idx = self._type_combo.currentIndex()
        if idx < 0 or idx >= len(_TYPE_FILTERS):
            return None
        return _TYPE_FILTERS[idx][1]

    def _on_search(self) -> None:
        profile_name = self._selected_profile_name()
        if not profile_name:
            self._status.setText("Pick a connection first.")
            return
        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            self._status.setText(
                "Selected connection isn't signed in yet. Open the connection "
                "manager to sign in."
            )
            return

        query = self._query_input.text().strip() or None
        type_filter = self._selected_type()

        self._search_seq += 1
        seq = self._search_seq
        self._search_btn.setEnabled(False)
        self._status.setText("Searching...")
        self._results.clear()

        def fetch(_handle) -> list[ItemSummary]:
            return list_items(
                profile,
                types=[type_filter] if type_filter else None,
                query=query,
            )

        def done(items: list[ItemSummary]) -> None:
            if seq != self._search_seq:
                return
            self._search_btn.setEnabled(True)
            self._render_results(items)

        def failed(exc: BaseException) -> None:
            if seq != self._search_seq:
                return
            _log.error("Search failed", exc_info=exc)
            self._search_btn.setEnabled(True)
            self._status.setText(f"Search failed: {format_error(exc)}")

        run_in_task("GratisGIS search", fetch, done, failed, cancelable=False)

    def _render_results(self, items: list[ItemSummary]) -> None:
        # Trim to types QGIS can actually consume so the result
        # list doesn't show items (forms, dashboards, web apps,
        # themes, templates, pick lists, boundaries) the user has
        # no add-to-canvas action for. Matches the Browser tree's
        # same filter (see browser/buckets.is_qgis_consumable).
        items = [it for it in items if is_qgis_consumable(it)]
        for it in sorted(items, key=lambda i: i.title.lower()):
            row = QListWidgetItem(_format_result_row(it))
            row.setData(Qt.ItemDataRole.UserRole, it.to_api_dict())
            row.setToolTip(_format_tooltip(it))
            self._results.addItem(row)
        self._status.setText(f"{len(items)} result(s).")

    def _on_double_click(self, item: QListWidgetItem) -> None:
        """Add the picked item to the QGIS canvas via the built-in
        OAPIF provider. Only supported for data_layer + tile_layer
        leaves today; other types pop a "no runtime here" message.
        """
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        try:
            summary = ItemSummary.from_api(payload)
        except Exception:  # pragma: no cover
            return
        profile_name = self._selected_profile_name()
        if not profile_name:
            return
        profile = self._store.get(profile_name)
        if profile is None:
            return

        # Accept both kebab and snake spellings the portal has
        # used over time.
        normalized_type = (summary.type or "").replace("-", "_")
        if normalized_type == "data_layer":
            self._add_data_layer(profile.portal_url, summary)
        elif normalized_type == "tile_layer":
            self._add_tile_layer(profile.portal_url, summary)
        else:
            QMessageBox.information(
                self,
                "Open in QGIS",
                f"'{summary.title}' is a {summary.type} item; the QGIS plugin "
                "doesn't yet open this type in the canvas. Use the Browser "
                "panel for the matching item types (data_layer, tile_layer).",
            )

    def _on_results_context_menu(self, _pos) -> None:
        """Right-click on a result row opens the item properties
        dialog. Single action menu for v1.
        """
        item = self._results.currentItem()
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        profile_name = self._selected_profile_name()
        profile = self._store.get(profile_name) if profile_name else None
        if profile is None:
            return
        # Lazy import keeps the dialog out of the search-dock
        # module's top-level import graph, so an error in the
        # properties dialog doesn't block opening the dock.
        from .item_properties_dialog import ItemPropertiesDialog

        dlg = ItemPropertiesDialog(
            self,
            profile=profile,
            item_id=payload.get("id", ""),
        )
        dlg.exec()

    def _add_data_layer(self, portal_url: str, summary: ItemSummary) -> None:
        self._iface.addVectorLayer(
            oapif_uri(portal_url, summary.id),
            summary.title,
            "OAPIF",
        )

    def _add_tile_layer(self, portal_url: str, summary: ItemSummary) -> None:
        self._iface.addVectorTileLayer(
            vector_tile_uri(portal_url, summary.id),
            summary.title,
        )


def _format_result_row(item: ItemSummary) -> str:
    return f"{item.title}  -  {item.type}  ({item.access})"


def _format_tooltip(item: ItemSummary) -> str:
    parts = [item.title, f"Type: {item.type}", f"Access: {item.access}"]
    if item.description:
        parts.append(item.description)
    if item.tags:
        parts.append("Tags: " + ", ".join(item.tags))
    parts.append(f"Updated: {item.updated_at.isoformat()}")
    return "\n".join(parts)
