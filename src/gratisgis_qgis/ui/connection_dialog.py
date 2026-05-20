# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection management dialog.

Phase 0 surface: list connections, add or edit a connection by
portal URL alone, sign in / sign out. The portal-info discovery
endpoint resolves everything else (display name, OIDC issuer, API
base URL) the moment the user clicks Add or Save, so the user never
sees "Keycloak realm" or "client id" anywhere in the UI.

The dialog is intentionally a thin wrapper over ``ConnectionStore``
and the portable client. The widgets here own no auth state; they
read the store, mutate it, and reload.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gratisgis_client import (
    AuthError,
    GratisGISClient,
    PortalDiscoveryError,
    discover,
)
from gratisgis_client.models.portal_info import PortalInfo

from ..auth_bridge import make_token_storage
from ..log import get_logger
from ..settings import ConnectionProfile, ConnectionStore

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
        outer.addWidget(QLabel("Configured portals:"))

        row = QHBoxLayout()
        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._update_button_state)
        self._list.itemDoubleClicked.connect(lambda _: self._on_edit())
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
            label = profile.display_label
            if label != name:
                label = f"{label}  ({name})"
            if profile.authcfg_id:
                label = f"{label}  [signed in]"
            self._list.addItem(label)
        self._update_button_state()

    def _selected_name(self) -> str | None:
        """Return the QSettings key for the selected row.

        The list shows enriched labels (portal name + key + signed-in
        flag), so we walk the store by index rather than parsing the
        label back out.
        """
        idx = self._list.currentRow()
        if idx < 0:
            return None
        names = self._store.list_names()
        if 0 <= idx < len(names):
            return names[idx]
        return None

    def _update_button_state(self) -> None:
        has_sel = self._list.currentRow() >= 0
        self._btn_edit.setEnabled(has_sel)
        self._btn_delete.setEnabled(has_sel)
        self._btn_signin.setEnabled(has_sel)
        self._btn_signout.setEnabled(has_sel)

    # ----- Actions -----

    def _on_new(self) -> None:
        _PortalEditDialog(self, store=self._store, initial=None).exec_()
        self._reload()

    def _on_edit(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        existing = self._store.get(name)
        if existing is None:
            return
        _PortalEditDialog(self, store=self._store, initial=existing).exec_()
        self._reload()

    def _on_delete(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        existing = self._store.get(name)
        if existing is None:
            return
        button = QMessageBox.question(
            self,
            "Delete connection?",
            f"Delete connection {existing.display_label!r}? Stored tokens will be cleared.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if button != QMessageBox.Yes:
            return
        if existing.authcfg_id:
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
        _refresh_discovery_and_sign_in(self, self._store, profile)
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

        cleared = ConnectionProfile(
            name=profile.name,
            portal_url=profile.portal_url,
            verify_tls=profile.verify_tls,
            authcfg_id="",
            portal_name=profile.portal_name,
            portal_version=profile.portal_version,
            api_base_url=profile.api_base_url,
            oidc_issuer=profile.oidc_issuer,
            discovered_at=profile.discovered_at,
        )
        self._store.save(cleared)
        self._reload()


def _normalize_portal_url(raw: str) -> str | None:
    """Return a tidy URL or None if the input is not parseable.

    Tidies: trims whitespace, adds https:// if no scheme is given,
    strips trailing slash. ``None`` covers both empty input and
    obviously malformed input; callers should show "please enter a
    portal URL".
    """
    url = raw.strip()
    if not url:
        return None
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url.rstrip("/")


def _discover_or_warn(
    parent: QWidget, portal_url: str, verify_tls: bool
) -> PortalInfo | None:
    """Run discovery, surface the user-visible error on failure.

    Returns the parsed ``PortalInfo`` on success, ``None`` after the
    warning has already been shown.
    """
    try:
        return asyncio.run(discover(portal_url, verify_tls=verify_tls))
    except PortalDiscoveryError as exc:
        QMessageBox.warning(
            parent,
            "Portal not reachable",
            f"This URL does not look like a GratisGIS portal.\n\n{exc}",
        )
        return None


def _run_sign_in(parent: QWidget, profile: ConnectionProfile) -> bool:
    """Run the PKCE flow against the cached discovery; show errors.

    Returns True on success, False on a user-visible failure that has
    already been surfaced via QMessageBox.
    """
    try:
        config = profile.to_portal_config()
    except ValueError as exc:
        QMessageBox.warning(parent, "Profile not configured", str(exc))
        return False
    storage = make_token_storage(profile.authcfg_id)

    async def _login() -> None:
        async with GratisGISClient(config, token_storage=storage) as client:
            await client.auth.login_interactive()

    try:
        asyncio.run(_login())
    except AuthError as exc:
        QMessageBox.warning(parent, "Sign-in failed", str(exc))
        return False
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("Unexpected sign-in error")
        QMessageBox.critical(parent, "Sign-in error", str(exc))
        return False
    return True


def _refresh_discovery_and_sign_in(
    parent: QWidget, store: ConnectionStore, profile: ConnectionProfile
) -> bool:
    """Discover fresh, save, sign in. Used by the main list's Sign In.

    Returns True on success.
    """
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        info = _discover_or_warn(parent, profile.portal_url, profile.verify_tls)
        if info is None:
            return False
        authcfg_id = profile.authcfg_id or ConnectionStore.new_authcfg_id()
        refreshed = ConnectionProfile(
            name=profile.name,
            portal_url=profile.portal_url,
            verify_tls=profile.verify_tls,
            authcfg_id=authcfg_id,
        ).with_discovery(info, now=time.time())
        store.save(refreshed)
        return _run_sign_in(parent, refreshed)
    finally:
        QApplication.restoreOverrideCursor()


class _PortalEditDialog(QDialog):
    """The Add / Edit dialog: one Portal URL field plus a Verify TLS
    checkbox.

    On Save, the dialog runs discovery against the URL. For new
    profiles it then kicks off a PKCE sign-in immediately so the
    user is signed in by the time the list refreshes. For edits, it
    only refreshes the cached discovery (sign-in stays on the main
    list's Sign in button).
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        store: ConnectionStore,
        initial: ConnectionProfile | None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._initial = initial
        self.setWindowTitle("Edit connection" if initial else "New connection")
        self.setMinimumWidth(480)
        self._build_ui()
        if initial is not None:
            self._portal.setText(initial.portal_url)
            self._verify.setChecked(initial.verify_tls)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.addWidget(
            QLabel("Enter the GratisGIS portal URL. Everything else is fetched from the portal.")
        )
        form = QFormLayout()
        self._portal = QLineEdit(placeholderText="https://gratisgis.org")
        form.addRow("Portal URL", self._portal)
        self._verify = QCheckBox("Verify TLS certificates")
        self._verify.setChecked(True)
        form.addRow("", self._verify)
        outer.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        save_label = "Save & Sign in" if self._initial is None else "Save"
        self._btn_save = QPushButton(save_label)
        self._btn_save.setDefault(True)
        self._btn_save.clicked.connect(self._on_save)
        buttons.addButton(self._btn_save, QDialogButtonBox.AcceptRole)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _on_save(self) -> None:
        url = _normalize_portal_url(self._portal.text())
        if url is None:
            QMessageBox.warning(
                self,
                "Portal URL required",
                "Please enter an http:// or https:// URL.",
            )
            return
        verify_tls = self._verify.isChecked()

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = _discover_or_warn(self, url, verify_tls)
            if info is None:
                return
            if self._initial is None:
                name = self._store.unique_name(info.name or urlparse(url).netloc)
                profile = ConnectionProfile(
                    name=name,
                    portal_url=url,
                    verify_tls=verify_tls,
                    authcfg_id=ConnectionStore.new_authcfg_id(),
                ).with_discovery(info, now=time.time())
                self._store.save(profile)
                # The provisional profile is on disk regardless of
                # sign-in outcome, so the user can retry from the
                # main list.
                _run_sign_in(self, profile)
            else:
                refreshed = ConnectionProfile(
                    name=self._initial.name,
                    portal_url=url,
                    verify_tls=verify_tls,
                    authcfg_id=self._initial.authcfg_id,
                ).with_discovery(info, now=time.time())
                self._store.save(refreshed)
        finally:
            QApplication.restoreOverrideCursor()
        self.accept()
