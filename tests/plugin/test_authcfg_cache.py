# SPDX-License-Identifier: AGPL-3.0-or-later
"""Making QGIS forget a credential it has already resolved.

Reported: signed out, and every portal layer on the canvas carried on
drawing.

Storing an auth config writes the auth database. It does not reach the
auth METHOD, which keeps the resolved header in memory keyed by authcfg
id and consults that cache, not the database, when it stamps a header
onto an outgoing request. So the database said "signed out" while the
wire kept sending the old key, for the rest of the session.

Both directions were wrong. Signing back in reuses the same authcfg id
on purpose, so that layers already pointing at it pick up the new key
without being rebuilt; without a cache clear they would keep sending
the key that had just been revoked instead.

Whether QGIS's cache actually behaves this way is a question for real
bindings, and ``scripts/qgis_smoke.py`` asks it. What is pinned here is
that the plugin makes the call, on every path that changes what an id
means, and that failing to make it cannot break a sign-out that has
already happened.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub


class _Manager:
    def __init__(self, *, store_ok: bool = True, clear_raises: bool = False) -> None:
        self.stored: list[str] = []
        self.removed: list[str] = []
        self.cleared: list[str] = []
        self.order: list[str] = []
        self._store_ok = store_ok
        self._clear_raises = clear_raises

    def storeAuthenticationConfig(  # QGIS API name
        self, cfg: Any, _overwrite: bool = False
    ) -> bool:
        self.stored.append(cfg.id())
        self.order.append("store")
        return self._store_ok

    def removeAuthenticationConfig(self, authcfg_id: str) -> bool:  # QGIS API
        self.removed.append(authcfg_id)
        self.order.append("remove")
        return True

    def clearCachedConfig(self, authcfg_id: str) -> None:  # QGIS API name
        if self._clear_raises:
            raise RuntimeError("auth manager is unhappy")
        self.cleared.append(authcfg_id)
        self.order.append("clear")

    def authMethodsKeys(self) -> list[str]:  # QGIS API name
        return ["APIHeader"]

    def loadAuthenticationConfig(  # QGIS API name
        self, _authcfg_id: str, _cfg: Any, _full: bool = False
    ) -> bool:
        return False


class _Config:
    def __init__(self) -> None:
        self._id = ""
        self._map: dict[str, str] = {}

    def setId(self, value: str) -> None:  # QGIS API name
        self._id = value

    def id(self) -> str:  # QGIS API name
        return self._id

    def setName(self, _value: str) -> None:  # QGIS API name
        pass

    def setMethod(self, _value: str) -> None:  # QGIS API name
        pass

    def setConfig(self, key: str, value: str) -> None:  # QGIS API name
        self._map[key] = value

    def configMap(self) -> dict[str, str]:  # QGIS API name
        return dict(self._map)


def _install(
    monkeypatch: pytest.MonkeyPatch, manager: _Manager
) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsApplication": type(
                    "QgsApplication",
                    (),
                    {"authManager": staticmethod(lambda: manager)},
                ),
                "QgsAuthMethodConfig": _Config,
            }
        },
    )
    import gratisgis_qgis.auth_bridge as m

    return m


class TestStoring:
    def test_storing_a_credential_clears_the_cached_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise a re-sign-in keeps sending the revoked key.

        The id is reused deliberately so existing layers pick up the
        new key. That only works if QGIS is told to forget the old one.
        """
        manager = _Manager()
        mod = _install(monkeypatch, manager)
        mod.store_api_header_authcfg(
            "cfg-1", name="x", method_key="APIHeader",
            headers={"Authorization": "Bearer new"},
        )
        assert manager.cleared == ["cfg-1"]

    def test_the_cache_is_cleared_after_the_write_not_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing first would let the old value be re-cached.

        Anything resolving the id between the clear and the write would
        read the database as it still was and put it straight back.
        """
        manager = _Manager()
        mod = _install(monkeypatch, manager)
        mod.store_api_header_authcfg(
            "cfg-1", name="x", method_key="APIHeader", headers={"A": "b"}
        )
        assert manager.order == ["store", "clear"]

    def test_a_failed_write_does_not_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing changed, so the cached copy is still correct.

        Clearing anyway would drop a working credential and break
        layers that were fine a moment ago.
        """
        manager = _Manager(store_ok=False)
        mod = _install(monkeypatch, manager)
        assert not mod.store_api_header_authcfg(
            "cfg-1", name="x", method_key="APIHeader", headers={"A": "b"}
        )
        assert manager.cleared == []


class TestRemoving:
    def test_removing_a_credential_clears_the_cached_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _Manager()
        mod = _install(monkeypatch, manager)
        mod.remove_authcfg("cfg-1")
        assert manager.cleared == ["cfg-1"]

    def test_the_cache_is_cleared_even_when_removal_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A credential that is deleted but still sent is the worst case.

        If the entry could not be removed, the cached copy is the only
        thing still able to authenticate, so it matters more here, not
        less.
        """
        class _Stuck(_Manager):
            def removeAuthenticationConfig(self, authcfg_id: str) -> bool:
                raise RuntimeError("locked")

        manager = _Stuck()
        mod = _install(monkeypatch, manager)
        mod.remove_authcfg("cfg-1")
        assert manager.cleared == ["cfg-1"]

    def test_an_empty_id_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _Manager()
        mod = _install(monkeypatch, manager)
        mod.remove_authcfg("")
        assert manager.cleared == []
        assert manager.removed == []


class TestForgetCachedAuthcfg:
    def test_it_reports_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _Manager()
        mod = _install(monkeypatch, manager)
        assert mod.forget_cached_authcfg("cfg-1") is True

    def test_a_build_that_raises_is_reported_not_propagated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every caller is finishing a sign-in or sign-out already done.

        Turning a completed action into an exception would leave the
        user with an error box for something that worked.
        """
        manager = _Manager(clear_raises=True)
        mod = _install(monkeypatch, manager)
        assert mod.forget_cached_authcfg("cfg-1") is False

    def test_without_qgis_it_is_simply_false(self) -> None:
        import gratisgis_qgis.auth_bridge as mod

        assert mod.forget_cached_authcfg("cfg-1") is False

    def test_an_empty_id_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = _Manager()
        mod = _install(monkeypatch, manager)
        assert mod.forget_cached_authcfg("") is False
        assert manager.cleared == []


class TestStoredSessionState:
    """The truth behind the "[signed in]" label.

    Found live: a profile whose authcfg pointer named a config that no
    longer existed in the auth database, so the label promised a
    session while every portal call demanded a fresh sign-in.
    """

    class _Storage:
        def __init__(self, tokens: Any) -> None:
            self._tokens = tokens

        def load(self) -> Any:
            return self._tokens

    def _state(
        self, monkeypatch: pytest.MonkeyPatch, tokens: Any
    ) -> str:
        from types import SimpleNamespace

        mod = _install(monkeypatch, _Manager())
        monkeypatch.setattr(
            mod, "make_token_storage", lambda _id: self._Storage(tokens)
        )
        return str(
            mod.stored_session_state(SimpleNamespace(authcfg_id="cfg-1"))
        )

    def test_live_tokens_read_as_signed_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        tokens = SimpleNamespace(refresh_is_stale=lambda: False)
        assert self._state(monkeypatch, tokens) == "signed-in"

    def test_a_lapsed_refresh_token_reads_as_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        tokens = SimpleNamespace(refresh_is_stale=lambda: True)
        assert self._state(monkeypatch, tokens) == "expired"

    def test_missing_tokens_read_as_signed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dangling-pointer case: authcfg id set, config gone."""
        assert self._state(monkeypatch, None) == "signed-out"

    def test_no_pointer_at_all_is_signed_out_without_a_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        mod = _install(monkeypatch, _Manager())

        def explode(_id: str) -> Any:
            raise AssertionError("must not touch storage")

        monkeypatch.setattr(mod, "make_token_storage", explode)
        state = mod.stored_session_state(SimpleNamespace(authcfg_id=""))
        assert state == "signed-out"

    def test_a_storage_error_degrades_to_signed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A locked auth database must not crash a list render."""
        from types import SimpleNamespace

        mod = _install(monkeypatch, _Manager())

        def boom(_id: str) -> Any:
            raise RuntimeError("auth db locked")

        monkeypatch.setattr(mod, "make_token_storage", boom)
        state = mod.stored_session_state(SimpleNamespace(authcfg_id="cfg-1"))
        assert state == "signed-out"
