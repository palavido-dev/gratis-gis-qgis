# SPDX-License-Identifier: AGPL-3.0-or-later
"""Item properties dialog (Phase 2).

Read-only metadata view for one portal item, opened from the
Browser tree's context menu and the Search dock's right-click
menu. Title / description / tags / sharing / created+updated
timestamps; expandable to edit-mode in a follow-up.

The fetch runs in a background task: the constructor shows the
"Loading..." scaffold immediately and the values fill in when the
task lands, so opening properties on a slow or dead portal never
freezes the QGIS main window.
"""
from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..log import get_logger
from ..portal import get_item
from ..settings import ConnectionProfile
from ..tasks import run_in_task

_log = get_logger(__name__)


class ItemPropertiesDialog(QDialog):
    """Modal dialog showing one item's metadata."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        profile: ConnectionProfile,
        item_id: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Item properties")
        self.setMinimumWidth(440)
        self._profile = profile
        self._item_id = item_id

        # ----- Layout scaffolding -----
        self._title = QLabel("Loading...")
        self._title.setStyleSheet("font-size: 14px; font-weight: 600;")
        self._type_row = QLabel("")
        self._type_row.setStyleSheet("color: #888;")

        self._form = QFormLayout()
        # Pre-populate with placeholder rows so the dialog has a
        # consistent height before the fetch returns; reduces the
        # visual jump when the values fill in.
        self._description = QTextEdit()
        self._description.setReadOnly(True)
        self._description.setFixedHeight(96)
        self._tags = QLabel("")
        self._tags.setWordWrap(True)
        self._access = QLabel("")
        self._created = QLabel("")
        self._updated = QLabel("")
        self._owner = QLabel("")
        self._id_label = QLabel(item_id)
        # Qt 6 / PyQt6 strict mode removed the unscoped
        # QLabel.TextSelectableByMouse shortcut; use the canonical
        # Qt.TextInteractionFlag scoped form (works on Qt 5 too).
        self._id_label.setTextInteractionFlags(
            self._id_label.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self._form.addRow("Description:", self._description)
        self._form.addRow("Tags:", self._tags)
        self._form.addRow("Access:", self._access)
        self._form.addRow("Owner:", self._owner)
        self._form.addRow("Created:", self._created)
        self._form.addRow("Updated:", self._updated)
        self._form.addRow("Item ID:", self._id_label)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout()
        layout.addWidget(self._title)
        layout.addWidget(self._type_row)
        layout.addLayout(self._form)
        layout.addLayout(btn_row)
        self.setLayout(layout)

        # Fetch off the GUI thread; the constructor must return with
        # the scaffold visible instead of blocking the main window on
        # one HTTP call.
        self._populate()

    def _populate(self) -> None:
        profile = self._profile
        item_id = self._item_id

        def fetch(_handle) -> dict[str, Any] | None:
            # get_item already swallows fetch errors to None with a
            # log entry; both outcomes render the same fallback text.
            return get_item(profile, item_id)

        def done(payload: dict[str, Any] | None) -> None:
            if payload is None:
                self._show_not_found()
                return
            _render_item(payload, self)

        def failed(exc: BaseException) -> None:  # pragma: no cover - defensive
            _log.error("Item properties fetch failed", exc_info=exc)
            self._show_not_found()

        run_in_task("GratisGIS item properties", fetch, done, failed, cancelable=False)

    def _show_not_found(self) -> None:
        self._title.setText("Item not found")
        self._type_row.setText(
            "The item may be private, deleted, or your session may have "
            "expired. Try refreshing the connection."
        )


def _owner_label(payload: dict[str, Any]) -> str:
    """Render the owner as a person, not a UUID.

    The portal ships ``owner: {id, username, fullName, avatarUrl}``.
    This used to read a flat ``ownerUsername`` that the portal has
    never sent, so it always fell through to the raw UUID. Prefers the
    full name with the username in brackets, since the username is what
    someone would search the portal by, and keeps the id as the last
    resort for an owner whose account is gone.
    """
    owner = payload.get("owner")
    username = full_name = ""
    if isinstance(owner, dict):
        username = str(owner.get("username") or "")
        full_name = str(owner.get("fullName") or "")
    if full_name and username:
        return f"{full_name} ({username})"
    return full_name or username or str(payload.get("ownerId") or "")


def _render_item(payload: dict[str, Any], dlg: ItemPropertiesDialog) -> None:
    """Fill the dialog widgets from the fetched item envelope.

    Kept out of the class body so it's easier to add per-type
    sections (e.g. data_layer schema preview) without growing the
    class itself.
    """
    # The payload is Item.to_api_dict() (portal.get_item), which
    # emits camelCase keys exclusively; no snake_case fallbacks.
    title = payload.get("title") or "(untitled)"
    type_ = payload.get("type") or "(unknown)"
    access = payload.get("access") or "(unknown)"
    description = payload.get("description") or ""
    tags = payload.get("tags") or []
    created = payload.get("createdAt") or ""
    updated = payload.get("updatedAt") or ""
    owner = _owner_label(payload)

    dlg._title.setText(title)
    dlg._type_row.setText(f"Type: {type_}")
    dlg._description.setPlainText(description)
    dlg._tags.setText(", ".join(tags) if tags else "(none)")
    dlg._access.setText(access)
    dlg._owner.setText(owner)
    dlg._created.setText(created)
    dlg._updated.setText(updated)
