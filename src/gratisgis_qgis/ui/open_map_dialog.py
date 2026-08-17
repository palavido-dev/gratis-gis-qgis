# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pick a portal map and open it in QGIS, from the toolbar.

The Browser tree and search already open maps by double-click; this
is the front-door version for someone who knows they want a map and
does not want to dig for it. One connection combo (hidden when only
one connection is signed in), the connection's maps, Open.
"""
from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..log import get_logger
from ..portal import list_items
from ..settings import ConnectionStore
from ..tasks import format_error, run_in_task

_log = get_logger(__name__)


def map_rows(items: list[Any]) -> list[tuple[str, str, str]]:
    """(item id, title, access) for every map in an item list, in the
    portal's order. Pure, so the filter is testable without Qt."""
    out: list[tuple[str, str, str]] = []
    for item in items:
        kind = (getattr(item, "type", "") or "").replace("-", "_")
        if kind != "map":
            continue
        out.append(
            (
                str(getattr(item, "id", "")),
                str(getattr(item, "title", "") or "Untitled map"),
                str(getattr(item, "access", "") or ""),
            )
        )
    return out


class OpenMapDialog(QDialog):
    """List the connection's maps; Open runs the open-map flow."""

    def __init__(self, iface: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._iface = iface
        self._store = ConnectionStore()
        self.setWindowTitle("Open GratisGIS map")
        self.setMinimumSize(420, 360)

        layout = QVBoxLayout(self)
        self._signed_in = [
            name
            for name in self._store.list_names()
            if (p := self._store.get(name)) is not None and p.authcfg_id
        ]
        self._connection_combo = QComboBox()
        for name in self._signed_in:
            self._connection_combo.addItem(name)
        if len(self._signed_in) > 1:
            layout.addWidget(QLabel("Portal:"))
            layout.addWidget(self._connection_combo)
        self._connection_combo.currentIndexChanged.connect(
            lambda _i: self._reload()
        )

        self._status = QLabel("")
        layout.addWidget(self._status)
        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _item: self._open())
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._open)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if not self._signed_in:
            self._status.setText(
                "Not signed in. Use Manage GratisGIS connections first."
            )
        else:
            self._reload()

    def _profile(self) -> Any:
        index = max(0, self._connection_combo.currentIndex())
        if not self._signed_in:
            return None
        name = self._signed_in[min(index, len(self._signed_in) - 1)]
        return self._store.get(name)

    def _reload(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        self._list.clear()
        self._status.setText("Loading maps...")

        def fetch(_handle: Any) -> list[Any]:
            return list_items(profile)

        def done(items: list[Any]) -> None:
            self._status.setText("")
            rows = map_rows(items)
            if not rows:
                self._status.setText(
                    "This portal has no maps you can see yet."
                )
                return
            for item_id, title, access in rows:
                row = QListWidgetItem(title)
                row.setData(Qt.ItemDataRole.UserRole, (item_id, title))
                row.setToolTip(f"Shared with: {access}" if access else "")
                self._list.addItem(row)

        def failed(exc: BaseException) -> None:
            self._status.setText(f"Could not load maps: {format_error(exc)}")

        run_in_task(
            "GratisGIS: list maps", fetch, done, failed, cancelable=False
        )

    def _open(self) -> None:
        current = self._list.currentItem()
        if current is None:
            self._status.setText("Pick a map first.")
            return
        payload = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        profile = self._profile()
        if profile is None:
            return
        item_id, title = payload
        from ..open_map import launch_open_map

        launch_open_map(profile, item_id, title, self._iface)
        self.accept()
