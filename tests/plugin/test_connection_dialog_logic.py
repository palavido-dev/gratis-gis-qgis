# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every decision the connection dialog makes, without a dialog.

This file sat at 18% covered while being the source of three shipped
bugs in one day, all of them about what gets written down when auth
state changes. The Qt wiring is not the risky part; what is persisted
after a sign-in, a cancelled sign-in, or a sign-out is.

So the decisions are pulled out of the Qt callbacks and asserted here
directly. Two of them, ``plan_sign_in_failure`` and ``resolve_sign_in``,
exist as named functions purely so this file can reach them; they used
to be closures inside ``_run_pkce_sign_in`` where nothing could.

``_signed_out`` has its own file, ``test_signout_teardown.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gratisgis_client import AuthError
from tests.plugin.conftest import ProfileFactory, install_qgis_stub


@pytest.fixture
def dialog_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.PyQt.QtCore": {
                "QSettings": type("QSettings", (), {}),
                "Qt": type("Qt", (), {}),
            },
            "qgis.PyQt.QtWidgets": {
                name: type(name, (), {})
                for name in (
                    "QApplication", "QCheckBox", "QDialog",
                    "QDialogButtonBox", "QFormLayout", "QHBoxLayout",
                    "QLabel", "QLineEdit", "QListWidget", "QMessageBox",
                    "QPushButton", "QVBoxLayout", "QWidget",
                )
            },
            "qgis.PyQt.QtGui": {"QIcon": type("QIcon", (), {})},
            "qgis.core": {
                "QgsApplication": type("QgsApplication", (), {}),
                "QgsAuthMethodConfig": type("QgsAuthMethodConfig", (), {}),
            },
            "qgis.utils": {"iface": None},
        },
    )
    import gratisgis_qgis.ui.connection_dialog as mod

    return mod


class _Tokens:
    """Only the part of TokenSet the profile update reads."""

    def __init__(self, sub: str | None) -> None:
        self._sub = sub

    def subject(self) -> str | None:
        return self._sub


class _Minted:
    def __init__(self, key_id: str = "key-9", token: str = "ggk_live") -> None:
        self.id = key_id
        self.token = token


class _List:
    """The parts of QListWidget the connection list uses."""

    def __init__(self, current: int = -1) -> None:
        self.items: list[str] = []
        self.current = current

    def clear(self) -> None:  # Qt API name
        self.items.clear()

    def addItem(self, label: str) -> None:  # Qt API name
        self.items.append(label)

    def currentRow(self) -> int:  # Qt API name
        return self.current

    def setEnabled(self, _value: bool) -> None:  # Qt API name
        pass


class _Store:
    def __init__(self, profiles: dict[str, Any]) -> None:
        self.profiles = dict(profiles)

    def list_names(self) -> list[str]:
        return sorted(self.profiles)

    def get(self, name: str) -> Any:
        return self.profiles.get(name)


class TestConnectionList:
    """What the list shows, and how a row maps back to a stored key.

    The label is enriched with the portal's own name and a signed-in
    flag, so it is not the QSettings key. Every action in the dialog
    resolves the row back to that key by index, which makes the mapping
    load bearing: get it wrong and Delete removes a different
    connection from the one highlighted.
    """

    def _dialog(self, dialog_mod: Any, store: _Store, current: int = -1) -> Any:
        dlg = dialog_mod.ConnectionManagerDialog.__new__(
            dialog_mod.ConnectionManagerDialog
        )
        dlg._store = store
        dlg._list = _List(current)
        for name in (
            "_btn_edit", "_btn_delete", "_btn_signin", "_btn_signout",
        ):
            setattr(dlg, name, SimpleNamespace(setEnabled=lambda _v: None))
        return dlg

    def test_a_signed_in_connection_is_marked(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        store = _Store({"demo": profile_factory(authcfg_id="auth-1")})
        dlg = self._dialog(dialog_mod, store)
        dlg._reload()
        assert "[signed in]" in dlg._list.items[0]

    def test_a_signed_out_connection_is_not(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """The flag is the only signed-in cue the dialog gives.

        Showing it for a signed-out connection is how a stale reserved
        authcfg used to look like a working session.
        """
        store = _Store({"demo": profile_factory(authcfg_id="")})
        dlg = self._dialog(dialog_mod, store)
        dlg._reload()
        assert "[signed in]" not in dlg._list.items[0]

    def test_the_portal_name_is_shown_with_the_local_key(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """Both, because they can differ and each answers a question.

        The portal name is what the user recognises; the key is what
        every error message and settings entry refers to.
        """
        store = _Store({
            "demo": profile_factory(name="demo", portal_name="GratisGIS")
        })
        dlg = self._dialog(dialog_mod, store)
        dlg._reload()
        assert "GratisGIS" in dlg._list.items[0]
        assert "demo" in dlg._list.items[0]

    def test_an_undiscovered_connection_shows_its_key_once(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """"demo (demo)" reads as a rendering bug."""
        store = _Store({"demo": profile_factory(name="demo", portal_name="")})
        dlg = self._dialog(dialog_mod, store)
        dlg._reload()
        assert dlg._list.items[0].count("demo") == 1

    def test_the_selected_row_maps_back_to_the_right_key(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """Rows are listed sorted, and resolved by the same order.

        Delete, Sign out and Edit all go through this. Resolving to the
        wrong key would act on a connection the user is not looking at.
        """
        store = _Store({
            "alpha": profile_factory(name="alpha"),
            "beta": profile_factory(name="beta"),
            "gamma": profile_factory(name="gamma"),
        })
        assert self._dialog(dialog_mod, store, 1)._selected_name() == "beta"

    def test_no_selection_resolves_to_nothing(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        store = _Store({"demo": profile_factory()})
        assert self._dialog(dialog_mod, store, -1)._selected_name() is None

    def test_a_row_beyond_the_store_resolves_to_nothing(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """The list and the store are read at different moments.

        A connection deleted between them would otherwise index past
        the end and raise inside a button handler.
        """
        store = _Store({"demo": profile_factory()})
        assert self._dialog(dialog_mod, store, 5)._selected_name() is None


class TestNormalizePortalUrl:
    """The only free-text field in the whole plugin."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://portal.example", "https://portal.example"),
            ("  https://portal.example  ", "https://portal.example"),
            ("https://portal.example/", "https://portal.example"),
            ("portal.example", "https://portal.example"),
            ("http://portal.example", "http://portal.example"),
        ],
    )
    def test_accepted_shapes(
        self, dialog_mod: Any, raw: str, expected: str
    ) -> None:
        assert dialog_mod._normalize_portal_url(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "https://", "ftp://x.example"])
    def test_rejected_shapes(self, dialog_mod: Any, raw: str) -> None:
        """None means the dialog asks again rather than saving junk."""
        assert dialog_mod._normalize_portal_url(raw) is None

    def test_a_scheme_is_added_not_assumed_away(self, dialog_mod: Any) -> None:
        """Bare hostnames get https, never http.

        The plugin sends a bearer token to whatever this resolves to.
        Defaulting to http would put it on the wire in clear.
        """
        assert dialog_mod._normalize_portal_url("portal.example").startswith(
            "https://"
        )


class TestWithUserId:
    def test_the_sub_claim_is_stamped_on(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        out = dialog_mod._with_user_id(
            profile_factory(user_id=""), _Tokens("user-42")
        )
        assert out.user_id == "user-42"

    def test_an_undecodable_token_leaves_the_old_id_alone(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """Losing the id would break the My Content bucket silently.

        The filter falls back to inferring the caller from ownership,
        which reports nothing for a user who owns no private items.
        """
        out = dialog_mod._with_user_id(
            profile_factory(user_id="user-1"), _Tokens(None)
        )
        assert out.user_id == "user-1"


class TestPlanSignInFailure:
    """What a failed sign-in tells the user, and what it writes down."""

    def test_cancelling_is_not_an_error(self, dialog_mod: Any) -> None:
        """No box. The user clicked Cancel; they know."""
        plan = dialog_mod.plan_sign_in_failure(
            dialog_mod.TaskCancelledError("cancelled"),
            clear_authcfg_on_failure=False,
        )
        assert plan.level == "none"
        assert not plan.log_unexpected

    def test_an_auth_error_is_a_warning_with_the_servers_words(
        self, dialog_mod: Any
    ) -> None:
        """The portal's own message, not a generic one.

        A rewritten message costs a database query to diagnose later;
        that lesson was learned on a raster publish that 400'd.
        """
        plan = dialog_mod.plan_sign_in_failure(
            AuthError("realm rejected the client"),
            clear_authcfg_on_failure=False,
        )
        assert plan.level == "warning"
        assert "realm rejected the client" in plan.text
        assert not plan.log_unexpected

    def test_an_unexpected_error_is_critical_and_logged(
        self, dialog_mod: Any
    ) -> None:
        """Logged with a traceback, because nobody predicted it."""
        plan = dialog_mod.plan_sign_in_failure(
            RuntimeError("socket exploded"), clear_authcfg_on_failure=False
        )
        assert plan.level == "critical"
        assert plan.log_unexpected

    @pytest.mark.parametrize(
        "exc",
        [AuthError("no"), RuntimeError("no"), None],
        ids=["auth", "unexpected", "cancelled"],
    )
    def test_a_reserved_authcfg_is_cleared_however_it_failed(
        self, dialog_mod: Any, exc: BaseException | None
    ) -> None:
        """Including cancellation, which is the easy one to get wrong.

        The id was reserved for an attempt that never produced a
        session. Leaving it behind shows the connection as signed in
        when it is not, and cancelling is the most likely way a first
        attempt ends.
        """
        error = exc or dialog_mod.TaskCancelledError("cancelled")
        plan = dialog_mod.plan_sign_in_failure(
            error, clear_authcfg_on_failure=True
        )
        assert plan.clear_authcfg

    @pytest.mark.parametrize(
        "exc",
        [AuthError("no"), RuntimeError("no"), None],
        ids=["auth", "unexpected", "cancelled"],
    )
    def test_an_existing_session_is_never_cleared_by_a_failure(
        self, dialog_mod: Any, exc: BaseException | None
    ) -> None:
        """Re-signing in and cancelling must not sign you out.

        The mirror of the test above, and the reason the flag exists
        rather than the failure path always clearing.
        """
        error = exc or dialog_mod.TaskCancelledError("cancelled")
        plan = dialog_mod.plan_sign_in_failure(
            error, clear_authcfg_on_failure=False
        )
        assert not plan.clear_authcfg


class TestResolveSignIn:
    """What a successful sign-in persists."""

    @pytest.fixture(autouse=True)
    def _no_qgis_calls(
        self, dialog_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            dialog_mod, "store_api_header_authcfg", lambda *a, **k: True
        )
        monkeypatch.setattr(dialog_mod, "remove_authcfg", lambda *a, **k: None)

    def _outcome(self, dialog_mod: Any, **overrides: Any) -> Any:
        base = {
            "tokens": _Tokens("user-42"),
            "minted": _Minted(),
            "mint_error": "",
        }
        base.update(overrides)
        return dialog_mod._SignInOutcome(**base)

    def test_the_happy_path_records_both_ids_and_the_user(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        profile, warning = dialog_mod.resolve_sign_in(
            profile_factory(user_id="", api_key_id="", layer_authcfg_id=""),
            self._outcome(dialog_mod),
            "APIHeader",
        )
        assert warning is None
        assert profile.user_id == "user-42"
        assert profile.api_key_id == "key-9"
        assert profile.layer_authcfg_id

    def test_the_existing_layer_authcfg_id_is_reused(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """So layers and projects already naming it start working again.

        This is the other half of sign-out keeping the entry: reusing
        the id on the way back in is what actually revives them.
        """
        profile, _ = dialog_mod.resolve_sign_in(
            profile_factory(layer_authcfg_id="lay-1"),
            self._outcome(dialog_mod),
            "APIHeader",
        )
        assert profile.layer_authcfg_id == "lay-1"

    def test_no_api_header_method_degrades_with_a_plain_warning(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        """Sign-in still succeeds; private layers just will not draw.

        A failed sign-in would be the worse outcome, and the message
        has to say what the user loses without naming an internal
        mechanism they cannot act on.
        """
        profile, warning = dialog_mod.resolve_sign_in(
            profile_factory(),
            self._outcome(dialog_mod, minted=None),
            None,
        )
        assert warning is not None
        assert "private layers" in warning.lower()
        assert profile.api_key_id == ""

    def test_a_failed_mint_says_why_and_leaves_no_half_state(
        self, dialog_mod: Any, profile_factory: ProfileFactory
    ) -> None:
        profile, warning = dialog_mod.resolve_sign_in(
            profile_factory(),
            self._outcome(
                dialog_mod, minted=None, mint_error="the portal said no."
            ),
            "APIHeader",
        )
        assert warning is not None
        assert "the portal said no." in warning
        assert profile.api_key_id == ""
        assert profile.layer_authcfg_id == ""

    def test_a_key_that_cannot_be_stored_is_revoked_not_stranded(
        self, dialog_mod: Any, profile_factory: ProfileFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A live credential nothing local remembers is the bad case.

        It would sit valid on the portal for a year with no way to
        revoke it from here, since the id is about to be dropped.
        """
        monkeypatch.setattr(
            dialog_mod, "store_api_header_authcfg", lambda *a, **k: False
        )
        revoked: list[str] = []
        monkeypatch.setattr(
            dialog_mod,
            "_revoke_key_in_background",
            lambda _p, key_id: revoked.append(key_id),
        )
        profile, warning = dialog_mod.resolve_sign_in(
            profile_factory(), self._outcome(dialog_mod), "APIHeader"
        )
        assert revoked == ["key-9"]
        assert warning is not None
        assert profile.api_key_id == ""

    @pytest.mark.parametrize(
        "outcome_kw,method",
        [
            ({}, "APIHeader"),
            ({"minted": None}, None),
            ({"minted": None, "mint_error": "no."}, "APIHeader"),
        ],
        ids=["stored", "no-method", "mint-failed"],
    )
    def test_the_user_id_survives_the_layer_key_step(
        self,
        dialog_mod: Any,
        profile_factory: ProfileFactory,
        outcome_kw: dict[str, Any],
        method: str | None,
    ) -> None:
        """The sub claim must not be lost on the way through, any path.

        Both steps of ``resolve_sign_in`` copy the profile with
        ``replace``, so each keeps what the other set. That is why the
        order of the two does not matter, and it stops being true the
        moment either builds a ConnectionProfile from scratch instead.
        Then the claim goes missing on every sign-in, and the only
        symptom is the My Content bucket quietly reporting nothing for
        users who own no private items.

        Run over the degraded paths too, since those are the ones that
        rebuild the profile most and get the least attention.
        """
        profile, _ = dialog_mod.resolve_sign_in(
            profile_factory(user_id=""),
            self._outcome(dialog_mod, **outcome_kw),
            method,
        )
        assert profile.user_id == "user-42"


def _record(dialog_mod: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swap the canvas reload for a list of the ids it was asked for."""
    calls: list[str] = []

    def reload(authcfg_id: str) -> int:
        calls.append(authcfg_id)
        return 0

    monkeypatch.setattr(dialog_mod, "reload_layers_using", reload)
    return calls


class _SaveStore:
    """A store that only has to remember the last profile saved."""

    def __init__(self) -> None:
        self.saved: list[Any] = []

    def save(self, profile: Any) -> None:
        self.saved.append(profile)


class TestSignInCompletion:
    """What ``_run_pkce_sign_in`` does once the browser flow returns.

    The sign-in path is reached from two places, the Add dialog and the
    main list's Sign In button, and both hand it the same completion
    callback. Asserted through the real function with its background
    task run inline, so the callback body itself is under test rather
    than a copy of it.
    """

    @pytest.fixture
    def run(
        self, dialog_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> Any:
        """Drive ``_run_pkce_sign_in`` to its success callback."""
        monkeypatch.setattr(
            dialog_mod, "find_api_header_method", lambda: "APIHeader"
        )
        monkeypatch.setattr(
            dialog_mod, "_TaskProgressDialog",
            lambda *a, **k: SimpleNamespace(
                show=lambda: None, finish=lambda: None
            ),
        )
        monkeypatch.setattr(
            dialog_mod, "QMessageBox",
            SimpleNamespace(warning=lambda *a: None, critical=lambda *a: None),
        )
        monkeypatch.setattr(
            dialog_mod, "resolve_sign_in",
            lambda profile, _outcome, _method: (profile, None),
        )

        def run_inline(_name: str, work: Any, done: Any, _failed: Any) -> Any:
            done(work(SimpleNamespace(is_canceled=lambda: False)))
            return SimpleNamespace(cancel=lambda: None)

        monkeypatch.setattr(dialog_mod, "run_in_task", run_inline)
        monkeypatch.setattr(dialog_mod, "get_client", lambda _p: _FakeClient())
        monkeypatch.setattr(dialog_mod, "revoke_layer_key", lambda *a: None)
        monkeypatch.setattr(dialog_mod, "mint_layer_key", lambda *a: _Minted())

        def go(profile: Any) -> _SaveStore:
            store = _SaveStore()
            dialog_mod._run_pkce_sign_in(
                None, store, profile,
                clear_authcfg_on_failure=False,
                on_finished=lambda _ok: None,
            )
            return store

        return go

    def test_layers_on_the_canvas_are_reloaded(
        self,
        dialog_mod: Any,
        monkeypatch: pytest.MonkeyPatch,
        run: Any,
        profile_factory: ProfileFactory,
    ) -> None:
        """The reported half of the bug that survived the first fix.

        Signing out stopped the layers drawing, and signing back in
        brought the vector layer back but left the two rasters blank.
        Storing a fresh key under the same authcfg id fixes what the
        auth manager hands out; a provider already holding a resolved
        copy needs telling.
        """
        calls = _record(dialog_mod, monkeypatch)
        run(profile_factory(layer_authcfg_id="lay-1"))
        assert calls == ["lay-1"]

    def test_the_id_comes_from_the_saved_profile(
        self,
        dialog_mod: Any,
        monkeypatch: pytest.MonkeyPatch,
        run: Any,
        profile_factory: ProfileFactory,
    ) -> None:
        """A first sign-in mints the id during this very callback.

        Reading it off the profile passed in would reload nothing at
        all on the path where the connection had no layer credential
        yet, which is exactly the path that just created one.
        """
        monkeypatch.setattr(
            dialog_mod, "resolve_sign_in",
            lambda profile, _o, _m: (
                profile.__class__(**{
                    **profile.__dict__, "layer_authcfg_id": "minted-1",
                }),
                None,
            ),
        )
        calls = _record(dialog_mod, monkeypatch)
        run(profile_factory(layer_authcfg_id=""))
        assert calls == ["minted-1"]

    def test_the_reload_happens_after_the_profile_is_saved(
        self,
        dialog_mod: Any,
        monkeypatch: pytest.MonkeyPatch,
        run: Any,
        profile_factory: ProfileFactory,
    ) -> None:
        """So a redraw that reads the store sees the new credential."""
        order: list[str] = []

        def reload(_authcfg_id: str) -> int:
            order.append("reload")
            return 0

        monkeypatch.setattr(dialog_mod, "reload_layers_using", reload)
        store = _SaveStore()
        real_save = store.save

        def spy_save(profile: Any) -> None:
            order.append("save")
            real_save(profile)

        store.save = spy_save  # type: ignore[method-assign]
        dialog_mod._run_pkce_sign_in(
            None, store, profile_factory(layer_authcfg_id="lay-1"),
            clear_authcfg_on_failure=False,
            on_finished=lambda _ok: None,
        )
        assert order == ["save", "reload"]


class _FakeClient:
    """Just enough client for the sign-in worker to run."""

    def __init__(self) -> None:
        self.auth = SimpleNamespace(
            login_interactive=lambda cancel: _Tokens("user-42")
        )
