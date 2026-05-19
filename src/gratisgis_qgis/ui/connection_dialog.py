# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection management dialog.

Phase 0 surface: list connections, add/edit/delete, sign in / sign
out. The actual sign-in spins up the PKCE flow, opens the user's
default browser, waits for the loopback callback, and stores tokens
via the QGIS auth manager bridge.

This dialog is intentionally a thin wrapper over ``ConnectionStore``
and the portable client. The widgets here own no auth state; they
read the store, mutate it, and reload.
"""

from __future__ import annotations

import asyncio

from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gratisgis_client import GratisGISClient
from gratisgis_client.errors import AuthError
from gratisgis_qgis.auth_bridge import make_token_storage
from gratisgis_qgis.log import get_logger
from gratisgis_qgis.settings import ConnectionProfile, ConnectionStore

_log = get_logger(__name__)


class ConnectionManagerDialog(QDialog):
    """Top-level dialog: list + add/edit/delete/sign-in/sign-out."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GratisGIS connections")
        self.setMinimumSize(540, 360)
        self._store = ConnectionStore()
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        label = QLabel("Configured portals:")
        outer.addWidget(label)

        row = QHBoxLayout()
        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._update_button_state)
        row.addWidget(self._list, stretch=2)

        side = QVBoxLayout()
        self._btn_new = QPushButton("New...")
        self._btn_new.clicked.connect(self._on_new)
        side.addWidget(self._btn_new)

        self._btn_edit = QPushButton("Edit...")
        self._btn_edit.clicked.connect(self._on_edit)
        side.addWidget(self._btn_edit)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete)
        side.addWidget(self._btn_delete)

        side.addSpacing(12)

        self._btn_signin = QPushButton("Sign in")
        self._btn_signin.clicked.connect(self._on_sign_in)
        side.addWidget(self._btn_signin)

        self._btn_signout = QPushButton("Sign out")
        self._btn_signout.clicked.connect(self._on_sign_out)
        side.addWidget(self._btn_signout)

        side.addStretch()
        row.addLayout(side, stretch=1)
        outer.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def _reload(self) -> None:
        self._list.clear()
        for name in self._store.list_names():
            profile = self._store.get(name)
            if profile is None:
                continue
            label = name
            if profile.authcfg_id:
                label = f"{name}  (signed in)"
            self._list.addItem(label)
        self._update_button_state()

    def _selected_name(self) -> str | None:
        items = self._list.selectedItems()
        if not items:
            return None
        # Strip the trailing "(signed in)" suffix if present.
        return items[0].text().split("  (", 1)[0]

    def _update_button_state(self) -> None:
        has_sel = self._list.currentRow() >= 0
        self._btn_edit.setEnabled(has_sel)
        self._btn_delete.setEnabled(has_sel)
        self._btn_signin.setEnabled(has_sel)
        self._btn_signout.setEnabled(has_sel)

    # ----- Actions -----

    def _on_new(self) -> None:
        profile = _ProfileEditDialog.new_profile(self)
        if profile is None:
            return
        if self._store.get(profile.name) is not None:
            QMessageBox.warning(self, "Name in use", f"A connection named {profile.name!r} already exists.")
            return
        self._store.save(profile)
        self._reload()

    def _on_edit(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        existing = self._store.get(name)
        if existing is None:
            return
        profile = _ProfileEditDialog.edit_profile(self, existing)
        if profile is None:
            return
        # Name is the QSettings key; renaming = delete + save.
        if profile.name != existing.name:
            self._store.delete(existing.name)
        self._store.save(profile)
        self._reload()

    def _on_delete(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        button = QMessageBox.question(
            self,
            "Delete connection?",
            f"Delete connection {name!r}? Stored tokens will be cleared.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if button != QMessageBox.Yes:
            return
        # Best-effort token cleanup.
        existing = self._store.get(name)
        if existing is not None and existing.authcfg_id:
            try:
                storage = make_token_storage(existing.authcfg_id)
                asyncio.run(storage.clear())
            except Exception:  # pragma: no cover - defensive
                _log.exception("Failed to clear tokens for %s", name)
        self._store.delete(name)
        self._reload()

    def _on_sign_in(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        profile = self._store.get(name)
        if profile is None:
            return
        # Ensure the profile has an authcfg id reserved before the flow.
        if not profile.authcfg_id:
            profile = ConnectionProfile(
                name=profile.name,
                portal_url=profile.portal_url,
                keycloak_url=profile.keycloak_url,
                realm=profile.realm,
                client_id=profile.client_id,
                authcfg_id=ConnectionStore.new_authcfg_id(),
                verify_tls=profile.verify_tls,
            )
            self._store.save(profile)

        async def _run() -> None:
            storage = make_token_storage(profile.authcfg_id)
            async with GratisGISClient(profile.to_portal_config(), token_storage=storage) as client:
                await client.auth.login_interactive()

        try:
            asyncio.run(_run())
        except AuthError as exc:
            QMessageBox.warning(self, "Sign-in failed", str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive
            _log.exception("Unexpected sign-in error")
            QMessageBox.critical(self, "Sign-in error", str(exc))
            return
        self._reload()

    def _on_sign_out(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        profile = self._store.get(name)
        if profile is None or not profile.authcfg_id:
            return

        async def _run() -> None:
            storage = make_token_storage(profile.authcfg_id)
            await storage.clear()

        try:
            asyncio.run(_run())
        except Exception as exc:  # pragma: no cover - defensive
            _log.exception("Sign-out failed")
            QMessageBox.warning(self, "Sign-out failed", str(exc))
            return

        # Clear the authcfg pointer on the profile so the list reflects state.
        cleared = ConnectionProfile(
            name=profile.name,
            portal_url=profile.portal_url,
            keycloak_url=profile.keycloak_url,
            realm=profile.realm,
            client_id=profile.client_id,
            authcfg_id="",
            verify_tls=profile.verify_tls,
        )
        self._store.save(cleared)
        self._reload()


class _ProfileEditDialog(QDialog):
    """Small form dialog: name, portal URL, Keycloak URL, realm, client id."""

    def __init__(self, parent: QWidget | None, initial: ConnectionProfile | None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit connection" if initial else "New connection")
        self.setMinimumWidth(440)
        self._initial = initial
        self._build_ui()
        if initial is not None:
            self._name.setText(initial.name)
            self._portal.setText(initial.portal_url)
            self._keycloak.setText(initial.keycloak_url)
            self._realm.setText(initial.realm)
            self._client_id.setText(initial.client_id)
            self._verify.setChecked(initial.verify_tls)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        form = QFormLayout()
        self._name = QLineEdit()
        self._portal = QLineEdit(placeholderText="https://gratisgis.org")
        self._keycloak = QLineEdit(placeholderText="https://gratisgis.org")
        self._realm = QLineEdit("gratis-gis")
        self._client_id = QLineEdit("qgis-plugin")
        self._verify = QCheckBox("Verify TLS certificates")
        self._verify.setChecked(True)
        form.addRow("Name", self._name)
        form.addRow("Portal URL", self._portal)
        form.addRow("Keycloak URL", self._keycloak)
        form.addRow("Realm", self._realm)
        form.addRow("Client id", self._client_id)
        form.addRow("", self._verify)
        outer.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def to_profile(self) -> ConnectionProfile | None:
        name = self._name.text().strip()
        portal = self._portal.text().strip()
        keycloak = self._keycloak.text().strip()
        realm = self._realm.text().strip() or "gratis-gis"
        client_id = self._client_id.text().strip() or "qgis-plugin"
        if not name or not portal or not keycloak:
            return None
        existing_authcfg = self._initial.authcfg_id if self._initial else ""
        return ConnectionProfile(
            name=name,
            portal_url=portal,
            keycloak_url=keycloak,
            realm=realm,
            client_id=client_id,
            authcfg_id=existing_authcfg,
            verify_tls=self._verify.isChecked(),
        )

    @classmethod
    def new_profile(cls, parent: QWidget | None) -> ConnectionProfile | None:
        dlg = cls(parent, None)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.to_profile()

    @classmethod
    def edit_profile(
        cls, parent: QWidget | None, initial: ConnectionProfile
    ) -> ConnectionProfile | None:
        dlg = cls(parent, initial)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.to_profile()


# Suppress unused-import warnings for symbols ruff cannot see through
# QGIS's dynamic Qt binding (Qt is used by some dialog flags above
# indirectly).
_ = Qt
_ = QInputDialog
