# SPDX-License-Identifier: AGPL-3.0-or-later
"""ConnectionStore round-trip tests over a fake QSettings.

The store writes each profile field to its own key; a field added to
the dataclass but missed in ``save`` / ``get`` silently loses state
across QGIS restarts (the exact failure would be a signed-in user
whose layer key evaporates on restart), so the round-trip is pinned
field by field.
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.plugin.conftest import ProfileFactory, install_qgis_stub


class _FakeQSettings:
    """Dict-backed stand-in for the slice of QSettings the store uses."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._group = ""

    def setValue(self, key: str, value: Any) -> None:  # Qt API name
        self._data[key] = value

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:  # Qt API name
        raw = self._data.get(key, default)
        if type is bool:
            return bool(raw)
        if type is str:
            return "" if raw is None else str(raw)
        if type is float:
            return float(raw)
        return raw

    def beginGroup(self, group: str) -> None:  # Qt API name
        self._group = group

    def endGroup(self) -> None:  # Qt API name
        self._group = ""

    def childGroups(self) -> list[str]:  # Qt API name
        prefix = f"{self._group}/"
        names = {
            key[len(prefix) :].split("/", 1)[0]
            for key in self._data
            if key.startswith(prefix)
        }
        return sorted(names)

    def remove(self, key: str) -> None:  # Qt API name
        for existing in [
            k for k in self._data if k == key or k.startswith(f"{key}/")
        ]:
            del self._data[existing]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Any:
    install_qgis_stub(
        monkeypatch,
        {"qgis.PyQt.QtCore": {"QSettings": _FakeQSettings}},
    )
    from gratisgis_qgis.settings import ConnectionStore

    return ConnectionStore(_FakeQSettings())


class TestRoundTrip:
    def test_all_fields_survive_save_and_get(
        self, store: Any, profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory(
            user_id="user-9",
            api_key_id="key-42",
            layer_authcfg_id="lyr1234",
        )
        store.save(profile)
        assert store.get(profile.name) == profile

    def test_new_fields_default_empty_for_older_profiles(
        self, store: Any, profile_factory: ProfileFactory
    ) -> None:
        # A profile saved by a plugin version predating the layer key
        # fields has no such keys in QSettings; loading must yield
        # empty strings (public-only rendering), not None or a crash.
        profile = profile_factory()
        store.save(profile)
        # Simulate the pre-Wave-3 on-disk shape by removing the keys.
        prefix = f"GratisGIS/connections/{profile.name}"
        store._s.remove(f"{prefix}/api_key_id")
        store._s.remove(f"{prefix}/layer_authcfg_id")
        loaded = store.get(profile.name)
        assert loaded is not None
        assert loaded.api_key_id == ""
        assert loaded.layer_authcfg_id == ""

    def test_list_names_sees_saved_profiles(
        self, store: Any, profile_factory: ProfileFactory
    ) -> None:
        store.save(profile_factory(name="alpha"))
        store.save(profile_factory(name="beta"))
        assert store.list_names() == ["alpha", "beta"]


class TestWithDiscovery:
    def test_preserves_identity_and_layer_key_fields(
        self, profile_factory: ProfileFactory
    ) -> None:
        from gratisgis_client.models.portal_info import (
            PortalApiInfo,
            PortalAuthInfo,
            PortalInfo,
        )

        profile = profile_factory(
            user_id="user-9",
            api_key_id="key-42",
            layer_authcfg_id="lyr1234",
        )
        info = PortalInfo(
            name="Fresh portal",
            version="0.9.99",
            api=PortalApiInfo(base_url="https://portal.example/api"),
            auth=PortalAuthInfo(
                type="oidc", issuer="https://portal.example/realms/gratis-gis"
            ),
        )
        refreshed = profile.with_discovery(info, now=456.0)
        # Discovery refresh must never drop who is signed in or the
        # layer key wiring; those change only at sign-in / sign-out.
        assert refreshed.user_id == "user-9"
        assert refreshed.api_key_id == "key-42"
        assert refreshed.layer_authcfg_id == "lyr1234"
        assert refreshed.portal_name == "Fresh portal"
        assert refreshed.discovered_at == 456.0
