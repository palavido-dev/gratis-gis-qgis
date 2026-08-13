# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the API Header authcfg helpers in ``auth_bridge``.

The auth manager is stubbed at the ``qgis.core`` seam. What matters:
the method probe finds QGIS's core API Header method by its exact
key (with a defensive case-insensitive fallback) and degrades to
None instead of raising, and the stored config carries the
Authorization header as a config-map entry, which is the shape
QgsAuthApiHeaderMethod applies as raw HTTP headers.
"""
from __future__ import annotations

import pytest

from gratisgis_qgis.auth_bridge import (
    find_api_header_method,
    remove_authcfg,
    store_api_header_authcfg,
)
from tests.plugin.conftest import install_qgis_stub


class _FakeAuthMethodConfig:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.method = ""
        self.config: dict[str, str] = {}

    def setId(self, value: str) -> None:  # QGIS API name
        self.id = value

    def setName(self, value: str) -> None:  # QGIS API name
        self.name = value

    def setMethod(self, value: str) -> None:  # QGIS API name
        self.method = value

    def setConfig(self, key: str, value: str) -> None:  # QGIS API name
        self.config[key] = value


class _FakeAuthManager:
    def __init__(self, keys: list[str], *, store_ok: bool = True) -> None:
        self._keys = keys
        self._store_ok = store_ok
        self.stored: list[_FakeAuthMethodConfig] = []
        self.removed: list[str] = []

    def authMethodsKeys(self) -> list[str]:  # QGIS API name
        return list(self._keys)

    def storeAuthenticationConfig(  # QGIS API name
        self, cfg: _FakeAuthMethodConfig, overwrite: bool
    ) -> bool:
        assert overwrite is True
        self.stored.append(cfg)
        return self._store_ok

    def removeAuthenticationConfig(self, authcfg_id: str) -> bool:  # QGIS API name
        self.removed.append(authcfg_id)
        return True


def _install(
    monkeypatch: pytest.MonkeyPatch, manager: _FakeAuthManager
) -> None:
    class _FakeQgsApplication:
        @staticmethod
        def authManager() -> _FakeAuthManager:  # QGIS API name
            return manager

    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsApplication": _FakeQgsApplication,
                "QgsAuthMethodConfig": _FakeAuthMethodConfig,
            }
        },
    )


class TestFindApiHeaderMethod:
    def test_exact_key_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _FakeAuthManager(["Basic", "APIHeader", "OAuth2"]))
        assert find_api_header_method() == "APIHeader"

    def test_case_variant_still_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Defensive: should the key ever be re-cased upstream, the
        # normalized comparison keeps finding it and returns the
        # RUNTIME spelling, which is what setMethod must receive.
        _install(monkeypatch, _FakeAuthManager(["Basic", "apiheader"]))
        assert find_api_header_method() == "apiheader"

    def test_absent_method_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _FakeAuthManager(["Basic", "OAuth2"]))
        assert find_api_header_method() is None

    def test_no_qgis_returns_none(self) -> None:
        # Outside QGIS the import fails; the probe must degrade to
        # None (public-only rendering) rather than raising into the
        # sign-in flow.
        assert find_api_header_method() is None


class TestStoreApiHeaderAuthcfg:
    def test_writes_method_and_header_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = _FakeAuthManager(["APIHeader"])
        _install(monkeypatch, manager)
        ok = store_api_header_authcfg(
            "lyr1234",
            name="GratisGIS layers: demo",
            method_key="APIHeader",
            headers={"Authorization": "Bearer ggk_secret"},
        )
        assert ok is True
        [cfg] = manager.stored
        assert cfg.id == "lyr1234"
        assert cfg.name == "GratisGIS layers: demo"
        assert cfg.method == "APIHeader"
        # The config map IS the header map for this method; each
        # entry becomes a raw HTTP header on authed requests.
        assert cfg.config == {"Authorization": "Bearer ggk_secret"}

    def test_store_refusal_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = _FakeAuthManager(["APIHeader"], store_ok=False)
        _install(monkeypatch, manager)
        ok = store_api_header_authcfg(
            "lyr1234",
            name="n",
            method_key="APIHeader",
            headers={"Authorization": "Bearer x"},
        )
        assert ok is False

    def test_no_qgis_returns_false(self) -> None:
        assert (
            store_api_header_authcfg(
                "lyr1234",
                name="n",
                method_key="APIHeader",
                headers={"Authorization": "Bearer x"},
            )
            is False
        )


class TestRemoveAuthcfg:
    def test_removes_by_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = _FakeAuthManager(["APIHeader"])
        _install(monkeypatch, manager)
        remove_authcfg("lyr1234")
        assert manager.removed == ["lyr1234"]

    def test_empty_id_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = _FakeAuthManager(["APIHeader"])
        _install(monkeypatch, manager)
        remove_authcfg("")
        assert manager.removed == []

    def test_no_qgis_swallows(self) -> None:
        # Teardown paths call this unconditionally; without QGIS it
        # must log and move on, never raise.
        remove_authcfg("lyr1234")
