# SPDX-License-Identifier: AGPL-3.0-or-later
"""Change who can see a portal item, from the Browser tree (#13).

Every publish ends with the same question, and until now answering it
meant opening the web portal. The dialog is three choices and a save;
the portal enforces who may change what, so the plugin's job is to
present the choice honestly and translate a refusal into a sentence.

The decision logic is pure and tested; the dialog carries it out.
"""
from __future__ import annotations

from typing import Any

from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..browser.refresh import refresh_browser_tree
from ..log import get_logger
from ..portal import get_client
from ..settings import ConnectionProfile
from ..tasks import format_error, run_in_task

_log = get_logger(__name__)

#: (portal value, label, what it means), in the order shown. Plain
#: words, no jargon: the audience is someone deciding who sees their
#: work, not someone who knows what an ACL is.
SHARING_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("private", "Only me", "Nobody else can see or use this item."),
    (
        "org",
        "My organization",
        "Everyone signed in to this portal can see and use it.",
    ),
    (
        "public",
        "Everyone",
        "Anyone can see and use it, signed in or not.",
    ),
)


def plan_sharing_change(current: str, chosen: str) -> str | None:
    """The access value to save, or None when nothing needs saving.

    None for the no-change case matters: a PATCH that rewrites the
    same value still bumps the item's updated stamp and reorders
    everyone's recency lists, which is a visible side effect of doing
    nothing.
    """
    valid = {value for value, _label, _desc in SHARING_CHOICES}
    if chosen not in valid:
        return None
    if chosen == current:
        return None
    return chosen


def plan_group_share_changes(
    current: list[str], chosen: list[str]
) -> tuple[list[str], list[str]]:
    """(groups to share with, groups to unshare), both sorted.

    A diff rather than a rewrite: only the boxes the user actually
    changed turn into portal calls, so an untouched group's share row
    (and any geographic limit an admin put on it) is never churned.
    """
    current_set = set(current)
    chosen_set = set(chosen)
    return (
        sorted(chosen_set - current_set),
        sorted(current_set - chosen_set),
    )


def sharing_error_text(exc: BaseException) -> str:
    """A refusal as a sentence about the situation, not the wire.

    403 is the one worth translating: it means the portal knows this
    item and this user, and the user is not allowed to reshare it.
    Everything else keeps the transport's wording via format_error.
    """
    text = format_error(exc)
    if "403" in text or "Forbidden" in text:
        return (
            "Only the item's owner (or an administrator) can change "
            "who sees it."
        )
    return text


class SharingDialog(QDialog):
    """Radio choice + save. The current access is preselected."""

    def __init__(
        self,
        profile: ConnectionProfile,
        item_id: str,
        item_title: str,
        current_access: str,
        parent: QWidget | None = None,
        *,
        groups: list[tuple[str, str]] | None = None,
        shared_group_ids: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._item_id = item_id
        self._current = current_access
        self._initial_groups = list(shared_group_ids or [])
        self.setWindowTitle(f"Sharing: {item_title}")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Who can see {item_title!r}?"))
        self._radios: list[tuple[str, QRadioButton]] = []
        for value, label, description in SHARING_CHOICES:
            radio = QRadioButton(label)
            radio.setToolTip(description)
            radio.setChecked(value == current_access)
            layout.addWidget(radio)
            note = QLabel(description)
            note.setStyleSheet("color: gray; margin-left: 22px;")
            layout.addWidget(note)
            self._radios.append((value, radio))

        # Group shares sit alongside the audience, not inside it: a
        # private item shared with a group is exactly how the portal
        # models "these people and nobody else".
        self._group_boxes: list[tuple[str, QCheckBox]] = []
        if groups:
            layout.addWidget(QLabel("Also share with these groups:"))
            for group_id, group_name in groups:
                box = QCheckBox(group_name)
                box.setChecked(group_id in self._initial_groups)
                layout.addWidget(box)
                self._group_boxes.append((group_id, box))
            note = QLabel(
                "Groups get view access. Finer permissions and "
                "geographic limits are managed in the portal."
            )
            note.setStyleSheet("color: gray;")
            note.setWordWrap(True)
            layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _chosen(self) -> str:
        for value, radio in self._radios:
            if radio.isChecked():
                return value
        return self._current

    def _chosen_groups(self) -> list[str]:
        return [
            group_id
            for group_id, box in self._group_boxes
            if box.isChecked()
        ]

    def _on_save(self) -> None:
        access = plan_sharing_change(self._current, self._chosen())
        to_share, to_unshare = plan_group_share_changes(
            self._initial_groups, self._chosen_groups()
        )
        if access is None and not to_share and not to_unshare:
            self.accept()
            return

        def save(_handle: Any) -> None:
            client = get_client(self._profile)
            if access is not None:
                client.items.update(self._item_id, access=access)
            for group_id in to_share:
                client.items.share_with_group(self._item_id, group_id)
            for group_id in to_unshare:
                client.items.unshare_with_group(self._item_id, group_id)

        def done(_result: Any) -> None:
            # The item may have moved between buckets (My Content
            # stays, but Public and Org listings change), so the tree
            # is refreshed rather than patched in place.
            refresh_browser_tree()
            self.accept()

        def failed(exc: BaseException) -> None:
            _log.error("sharing change failed", exc_info=exc)
            QMessageBox.warning(
                self, "Could not change sharing", sharing_error_text(exc)
            )

        run_in_task(
            "GratisGIS: change sharing", save, done, failed, cancelable=False
        )
