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

Network work (discovery, the PKCE browser round-trip) runs in
background tasks via ``..tasks``; the GUI thread only shows a small
progress surface with a working Cancel. The PKCE wait in particular
can sit for minutes while the user deals with the browser, so it
must never block the Qt event loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
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

from gratisgis_client import AuthError, PortalDiscoveryError, discover
from gratisgis_client.auth.tokens import TokenSet
from gratisgis_client.discovery import DiscoveryResult
from gratisgis_client.endpoints.api_keys import ApiKeyCreated

from ..auth_bridge import (
    find_api_header_method,
    make_token_storage,
    remove_authcfg,
    store_api_header_authcfg,
)
from ..layer_auth import mint_layer_key, revoke_layer_key
from ..log import get_logger
from ..portal import get_client, invalidate
from ..settings import ConnectionProfile, ConnectionStore
from ..tasks import TaskCancelledError, format_error, run_in_task

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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
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

    def _set_busy(self, busy: bool) -> None:
        """Lock the action surface while a background flow runs.

        Only the buttons + list lock; the dialog itself stays live
        so the sign-in progress child dialog keeps receiving events.
        """
        for widget in (
            self._list,
            self._btn_new,
            self._btn_edit,
            self._btn_delete,
            self._btn_signin,
            self._btn_signout,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._update_button_state()

    # ----- Actions -----

    def _on_new(self) -> None:
        _PortalEditDialog(self, store=self._store, initial=None).exec()
        self._reload()

    def _on_edit(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        existing = self._store.get(name)
        if existing is None:
            return
        _PortalEditDialog(self, store=self._store, initial=existing).exec()
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
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if button != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)

        def teardown() -> None:
            if existing.authcfg_id:
                invalidate(existing)
                try:
                    # Local auth-manager delete only; no network involved.
                    make_token_storage(existing.authcfg_id).clear()
                except Exception:  # pragma: no cover - defensive
                    _log.exception("Failed to clear tokens for %s", name)
            remove_authcfg(existing.layer_authcfg_id)
            self._store.delete(name)
            self._set_busy(False)
            self._reload()

        # Deleting a connection with its layer key still live would
        # leave a working credential on the portal that nothing local
        # remembers; revoke it first (best-effort), then tear down.
        _revoke_layer_key_then(existing, teardown)

    def _on_sign_in(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        profile = self._store.get(name)
        if profile is None:
            return
        self._set_busy(True)

        def finished(_ok: bool) -> None:
            self._set_busy(False)
            self._reload()

        _refresh_discovery_and_sign_in(self, self._store, profile, on_finished=finished)

    def _on_sign_out(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        profile = self._store.get(name)
        if profile is None or not profile.authcfg_id:
            return
        self._set_busy(True)

        def teardown() -> None:
            # Drop the shared client before its token storage goes
            # away; a cached client would otherwise keep serving the
            # old tokens.
            invalidate(profile)
            try:
                # Local-only: removes the token authcfg entry from
                # the QGIS auth manager. Keycloak's SSO session is
                # deliberately left alone (matches the pre-task
                # behavior).
                make_token_storage(profile.authcfg_id).clear()
            except Exception as exc:  # pragma: no cover - defensive
                _log.exception("Sign-out failed")
                QMessageBox.warning(self, "Sign-out failed", str(exc))
                self._set_busy(False)
                return
            remove_authcfg(profile.layer_authcfg_id)
            cleared = replace(
                profile, authcfg_id="", api_key_id="", layer_authcfg_id=""
            )
            self._store.save(cleared)
            self._set_busy(False)
            self._reload()

        # The layer key is revoked server-side first, while the OIDC
        # session that authorizes the revoke call still exists.
        _revoke_layer_key_then(profile, teardown)


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


class _TaskProgressDialog(QDialog):
    """Small modal surface shown while a background flow runs.

    One label plus a Cancel button. Esc, the window close button, and
    Cancel all funnel into the same cancel request; the dialog stays
    open showing "Cancelling..." until the flow's completion callback
    calls ``finish()``, because dismissing the window while the
    worker still runs would let the user start a second overlapping
    sign-in.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        text: str,
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(360)
        self._on_cancel = on_cancel
        self._cancel_requested = False

        self._label = QLabel(text)
        self._label.setWordWrap(True)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._btn_cancel)

        layout = QVBoxLayout()
        layout.addWidget(self._label)
        layout.addLayout(row)
        self.setLayout(layout)

    def set_text(self, text: str) -> None:
        if not self._cancel_requested:
            self._label.setText(text)

    def finish(self) -> None:
        """Close the dialog from the flow's completion callback."""
        self.accept()

    def reject(self) -> None:  # Qt override
        # Request cancellation instead of closing; the flow closes us
        # via finish() once the worker actually stops.
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self._label.setText("Cancelling...")
        self._btn_cancel.setEnabled(False)
        self._on_cancel()

    def closeEvent(self, event) -> None:  # Qt override
        event.ignore()
        self.reject()


def _with_user_id(profile: ConnectionProfile, tokens: TokenSet) -> ConnectionProfile:
    """Copy the profile with the signed-in user's id filled in.

    The ``sub`` claim gives the Browser tree an authoritative caller
    id for the My Content / Shared with Me buckets; without it the
    bucket filter has to infer the caller from item ownership, which
    fails for users who own no private items. Keeps the existing id
    when the token is not a decodable JWT.
    """
    sub = tokens.subject()
    if not sub or sub == profile.user_id:
        return profile
    return replace(profile, user_id=sub)


def _clear_authcfg(profile: ConnectionProfile) -> ConnectionProfile:
    """Copy of the profile with the signed-in marker removed."""
    return replace(profile, authcfg_id="")


@dataclass(frozen=True)
class _SignInOutcome:
    """What the sign-in worker hands back to the GUI callback."""

    tokens: TokenSet
    minted: ApiKeyCreated | None
    """The fresh read-only layer key, or None when minting was
    skipped (no API Header method) or failed."""
    mint_error: str
    """User-presentable reason when minting failed; empty otherwise."""


def _revoke_layer_key_then(
    profile: ConnectionProfile, continuation: Callable[[], None]
) -> None:
    """Revoke the connection's layer key server-side, then continue.

    Best-effort by design: the revoke is a network call that can fail
    on a dead session, and every caller (sign-out, profile delete) is
    a teardown that must complete locally regardless, so the done and
    error paths both funnel into ``continuation`` on the GUI thread.
    Profiles without a key skip straight to the continuation.
    """
    if not profile.api_key_id:
        continuation()
        return
    key_id = profile.api_key_id

    def revoke(_handle) -> None:
        revoke_layer_key(get_client(profile), key_id)

    run_in_task(
        "GratisGIS: revoke layer API key",
        revoke,
        lambda _result: continuation(),
        lambda _exc: continuation(),
        cancelable=False,
    )


def _clear_layer_key(profile: ConnectionProfile) -> ConnectionProfile:
    """Profile copy with no layer key, stale authcfg removed.

    Removing the auth-manager entry matters as much as clearing the
    fields: a lingering authcfg would either carry a revoked key
    (401s that read as a broken layer) or, worse, keep a live token
    on disk after the profile forgot it exists.
    """
    remove_authcfg(profile.layer_authcfg_id)
    return replace(profile, api_key_id="", layer_authcfg_id="")


def _apply_layer_key(
    profile: ConnectionProfile,
    outcome: _SignInOutcome,
    method_key: str | None,
) -> tuple[ConnectionProfile, str | None]:
    """Store the minted layer key; return (profile, warning line).

    On success the key lands in an API Header authcfg (reusing the
    connection's existing id so layer URIs already referencing it
    pick up the fresh key) and both ids are stamped on the profile.
    Every failure mode degrades to public-only rendering with a
    one-line warning for the user; sign-in itself never fails on
    this leg, because "private layers will not render" beats a
    failed sign-in.
    """
    if method_key is None:
        _log.warning(
            "API Header auth method unavailable; private layers stay on the "
            "public surface for %s",
            profile.name,
        )
        return _clear_layer_key(profile), (
            "This QGIS build has no 'API Header' authentication method, so "
            "private layers will not render on the map canvas. Public layers "
            "are unaffected."
        )
    if outcome.minted is None:
        return _clear_layer_key(profile), (
            f"Could not create the layer-rendering key: {outcome.mint_error} "
            "Private layers will not render until the next sign-in."
        )
    authcfg_id = profile.layer_authcfg_id or ConnectionStore.new_authcfg_id()
    stored = store_api_header_authcfg(
        authcfg_id,
        name=f"GratisGIS layers: {profile.name}",
        method_key=method_key,
        headers={"Authorization": f"Bearer {outcome.minted.token}"},
    )
    if not stored:
        # The key exists server-side but nothing can ever use it;
        # revoke it in the background rather than leaving a live
        # credential idling in the portal.
        _revoke_key_in_background(profile, outcome.minted.id)
        return _clear_layer_key(profile), (
            "Could not store the layer-rendering key in the QGIS auth "
            "database. Private layers will not render."
        )
    return (
        replace(profile, api_key_id=outcome.minted.id, layer_authcfg_id=authcfg_id),
        None,
    )


def _revoke_key_in_background(profile: ConnectionProfile, key_id: str) -> None:
    """Fire-and-forget revoke for a key that never became usable."""

    def revoke(_handle) -> None:
        revoke_layer_key(get_client(profile), key_id)

    # Outcome callbacks are no-ops on purpose: revoke_layer_key
    # already logs its own failures and there is no UI state to
    # update for a key the user never knew existed.
    run_in_task(
        "GratisGIS: revoke unusable API key",
        revoke,
        lambda _result: None,
        lambda _exc: None,
        cancelable=False,
    )


def _run_pkce_sign_in(
    parent: QWidget,
    store: ConnectionStore,
    profile: ConnectionProfile,
    *,
    clear_authcfg_on_failure: bool,
    on_finished: Callable[[bool], None],
) -> None:
    """Run the PKCE browser flow in a background task.

    Shows the progress dialog with a live Cancel wired to the flow's
    cancel probe (PKCEFlow polls it every 0.25 s). On success the
    profile is re-saved with the token's ``sub`` claim, and the same
    worker revokes the connection's previous layer key and mints a
    fresh read-only one for private layer rendering (the GUI callback
    stores it in an API Header authcfg; see ``_apply_layer_key`` for
    the degradation rules). On failure the reserved authcfg id is
    optionally cleared so the connection list does not show a stale
    "[signed in]" label.
    """
    try:
        # Fail fast on the GUI thread for undiscovered profiles; from
        # the worker this would surface as a generic task error
        # instead of the actionable "not configured" message.
        profile.to_portal_config()
    except ValueError as exc:
        QMessageBox.warning(parent, "Profile not configured", str(exc))
        on_finished(False)
        return

    # Probe for the API Header auth method on the GUI thread, before
    # the worker runs: when the method is missing there is no point
    # minting a key nothing can attach to a request.
    api_header_method = find_api_header_method()

    # The dialog's Cancel routes through the task's own cancel flag,
    # which PKCEFlow polls every 0.25 s via the probe below. Boxed
    # because the dialog is built before the task exists.
    controller_box: list[object] = []

    def request_cancel() -> None:
        for controller in controller_box:
            controller.cancel()  # type: ignore[attr-defined]

    progress = _TaskProgressDialog(
        parent,
        title="Signing in to GratisGIS",
        text=(
            "Complete the sign-in in your web browser.\n"
            "This window closes by itself when you are done."
        ),
        on_cancel=request_cancel,
    )

    def sign_in(handle) -> _SignInOutcome:
        client = get_client(profile)
        tokens = client.auth.login_interactive(cancel=handle.is_canceled)
        # The previous layer key (if any) is superseded whatever
        # happens next; revoke it now, while the fresh session
        # guarantees the call is authorized, so re-sign-ins never
        # accumulate keys server-side.
        revoke_layer_key(client, profile.api_key_id)
        minted: ApiKeyCreated | None = None
        mint_error = ""
        if api_header_method is not None:
            try:
                minted = mint_layer_key(client, profile.name)
            except Exception as exc:
                _log.exception("Layer API key mint failed")
                mint_error = format_error(exc)
        return _SignInOutcome(tokens=tokens, minted=minted, mint_error=mint_error)

    def done(outcome: _SignInOutcome) -> None:
        updated = _with_user_id(profile, outcome.tokens)
        updated, warn_line = _apply_layer_key(updated, outcome, api_header_method)
        store.save(updated)
        progress.finish()
        if warn_line:
            QMessageBox.warning(parent, "Private layer rendering", warn_line)
        on_finished(True)

    def failed(exc: BaseException) -> None:
        progress.finish()
        if isinstance(exc, TaskCancelledError):
            pass  # The user asked for it; no error surface.
        elif isinstance(exc, AuthError):
            QMessageBox.warning(parent, "Sign-in failed", str(exc))
        else:
            _log.error("Unexpected sign-in error", exc_info=exc)
            QMessageBox.critical(parent, "Sign-in error", str(exc))
        if clear_authcfg_on_failure:
            store.save(_clear_authcfg(profile))
        on_finished(False)

    controller_box.append(run_in_task("GratisGIS sign-in", sign_in, done, failed))
    progress.show()


def _run_discovery(
    parent: QWidget,
    portal_url: str,
    verify_tls: bool,
    *,
    on_done: Callable[[DiscoveryResult], None],
    on_failed: Callable[[], None],
) -> None:
    """Fetch portal-info in a background task; warn on failure.

    Not cancelable: the probe has a 10 s network timeout of its own,
    and a cancel that cannot actually interrupt the socket would just
    lie to the user. The wait cursor is the feedback for that bounded
    wait; the event loop stays live underneath it.
    """

    def fetch(_handle) -> DiscoveryResult:
        return discover(portal_url, verify_tls=verify_tls)

    def done(result: DiscoveryResult) -> None:
        QApplication.restoreOverrideCursor()
        on_done(result)

    def failed(exc: BaseException) -> None:
        QApplication.restoreOverrideCursor()
        if isinstance(exc, PortalDiscoveryError):
            QMessageBox.warning(
                parent,
                "Portal not reachable",
                f"This URL does not look like a GratisGIS portal.\n\n{exc}",
            )
        else:  # pragma: no cover - defensive
            _log.error("Unexpected discovery error", exc_info=exc)
            QMessageBox.critical(parent, "Discovery error", str(exc))
        on_failed()

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    run_in_task("GratisGIS portal discovery", fetch, done, failed, cancelable=False)


def _refresh_discovery_and_sign_in(
    parent: QWidget,
    store: ConnectionStore,
    profile: ConnectionProfile,
    *,
    on_finished: Callable[[bool], None],
) -> None:
    """Discover fresh, save, sign in. Used by the main list's Sign In.

    ``on_finished(ok)`` fires exactly once, on the GUI thread, after
    the whole flow ends (success, failure, or cancel). On failure,
    if the profile didn't have a prior authcfg_id (so the user wasn't
    already signed in), the newly reserved id is cleared so the list
    does not show a stale "[signed in]" label.
    """
    had_prior_auth = bool(profile.authcfg_id)

    def discovered(result: DiscoveryResult) -> None:
        # Adopt the canonical post-redirect URL on every re-sign-in
        # so a profile that was originally saved against the www
        # alias quietly upgrades to the canonical host. replace()
        # keeps every other field (user_id, the layer key ids the
        # sign-in worker needs for revocation) intact.
        canonical_url = result.portal_url
        authcfg_id = profile.authcfg_id or ConnectionStore.new_authcfg_id()
        refreshed = replace(
            profile,
            portal_url=canonical_url,
            authcfg_id=authcfg_id,
        ).with_discovery(result.info, now=time.time())
        store.save(refreshed)
        # The rediscovered config may differ from whatever a cached
        # client was built with; force a rebuild for the sign-in.
        invalidate(profile)
        invalidate(refreshed)
        _run_pkce_sign_in(
            parent,
            store,
            refreshed,
            clear_authcfg_on_failure=not had_prior_auth,
            on_finished=on_finished,
        )

    _run_discovery(
        parent,
        profile.portal_url,
        profile.verify_tls,
        on_done=discovered,
        on_failed=lambda: on_finished(False),
    )


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
        self._busy = False
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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        save_label = "Save & Sign in" if self._initial is None else "Save"
        self._btn_save = QPushButton(save_label)
        self._btn_save.setDefault(True)
        self._btn_save.clicked.connect(self._on_save)
        buttons.addButton(self._btn_save, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._btn_save.setEnabled(not busy)
        self._portal.setEnabled(not busy)
        self._verify.setEnabled(not busy)

    def reject(self) -> None:  # Qt override
        # No closing out from under a running discovery / sign-in;
        # the completion callbacks re-enable or accept.
        if self._busy:
            return
        super().reject()

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
        self._set_busy(True)
        _run_discovery(
            self,
            url,
            verify_tls,
            on_done=lambda result: self._on_discovered(result, verify_tls),
            on_failed=lambda: self._set_busy(False),
        )

    def _on_discovered(self, result: DiscoveryResult, verify_tls: bool) -> None:
        info = result.info
        # Use the canonical post-redirect URL so subsequent API
        # calls don't pay a 301 round-trip per request.
        canonical_url = result.portal_url
        if self._initial is None:
            name = self._store.unique_name(info.name or urlparse(canonical_url).netloc)
            profile = ConnectionProfile(
                name=name,
                portal_url=canonical_url,
                verify_tls=verify_tls,
                authcfg_id=ConnectionStore.new_authcfg_id(),
            ).with_discovery(info, now=time.time())
            self._store.save(profile)
            invalidate(profile)

            # The provisional profile is on disk regardless of
            # sign-in outcome, so the user can retry from the
            # main list. If PKCE fails, the reserved authcfg_id is
            # cleared so the list doesn't falsely show the
            # connection as signed in.
            def finished(_ok: bool) -> None:
                self._set_busy(False)
                self.accept()

            _run_pkce_sign_in(
                self,
                self._store,
                profile,
                clear_authcfg_on_failure=True,
                on_finished=finished,
            )
            return

        # replace() keeps user_id and the layer key ids intact; an
        # edit only touches the URL and TLS mode.
        refreshed = replace(
            self._initial,
            portal_url=canonical_url,
            verify_tls=verify_tls,
        ).with_discovery(info, now=time.time())
        self._store.save(refreshed)
        # The edit may have changed the URL or TLS mode; a client
        # cached under the old settings must not survive it.
        invalidate(self._initial)
        invalidate(refreshed)
        self._set_busy(False)
        self.accept()
