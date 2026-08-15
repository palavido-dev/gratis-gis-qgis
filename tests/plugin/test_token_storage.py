# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the OIDC session actually lives.

``QgisAuthManagerTokenStorage`` holds every refresh token the plugin
has, inside a QGIS authcfg that QGIS encrypts at rest. It had no test,
which is uncomfortable for the one component whose failure modes are
"the user is signed out for no reason" and "a token is left on disk
after the profile forgot it exists".

Both halves matter and neither raises. A read that returns None too
eagerly signs the user out; a read that raises breaks whatever call
happened to run first, which is how a failed refresh surfaced as an
unrelated error rather than "your session ended, sign in again".
"""
from __future__ import annotations

import json
from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub


class _Config:
    """QgsAuthMethodConfig, holding a config map like the real one."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._id = ""
        self._name = ""
        self._method = ""
        self.valid = True

    def setId(self, value: str) -> None:  # QGIS API name
        self._id = value

    def id(self) -> str:  # QGIS API name
        return self._id

    def setName(self, value: str) -> None:  # QGIS API name
        self._name = value

    def name(self) -> str:  # QGIS API name
        return self._name

    def setMethod(self, value: str) -> None:  # QGIS API name
        self._method = value

    def method(self) -> str:  # QGIS API name
        return self._method

    def setConfig(self, key: str, value: str) -> None:  # QGIS API name
        self._map[key] = value

    def config(self, key: str) -> str:  # QGIS API name
        return self._map.get(key, "")

    def configMap(self) -> dict[str, str]:  # QGIS API name
        return dict(self._map)

    def isValid(self) -> bool:  # QGIS API name
        return self.valid


class _AuthManager:
    """The subset of QgsAuthManager the storage touches."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, str]] = {}
        self.store_ok = True
        self.load_ok = True

    def storeAuthenticationConfig(  # QGIS API name
        self, cfg: _Config, _overwrite: bool = False
    ) -> bool:
        if not self.store_ok:
            return False
        self.entries[cfg.id()] = cfg.configMap()
        return True

    def loadAuthenticationConfig(  # QGIS API name
        self, authcfg_id: str, cfg: _Config, _full: bool = False
    ) -> bool:
        if not self.load_ok or authcfg_id not in self.entries:
            return False
        for key, value in self.entries[authcfg_id].items():
            cfg.setConfig(key, value)
        return True

    def removeAuthenticationConfig(self, authcfg_id: str) -> bool:  # QGIS API
        return self.entries.pop(authcfg_id, None) is not None


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, _AuthManager]:
    manager = _AuthManager()
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

    return m, manager


def _tokens() -> Any:
    from gratisgis_client.auth.tokens import TokenSet

    return TokenSet(
        access_token="at",
        refresh_token="rt",
        access_expires_at=1893456000.0,
        refresh_expires_at=1893456900.0,
        id_token="it",
        scope="openid offline_access",
    )


class TestRoundTrip:
    def test_a_saved_token_set_comes_back_intact(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """Every field, not just the refresh token.

        Losing the expiry stamps would make the client refresh on every
        call; losing id_token loses the sub claim the Browser tree's My
        Content bucket depends on; losing scope loses the record that
        offline_access was granted, which is the difference between a
        session that survives a restart and one that dies in an hour.
        """
        mod, _ = bridge
        storage = mod.QgisAuthManagerTokenStorage("cfg-1")
        storage.save(_tokens())
        loaded = storage.load()
        assert loaded == _tokens()

    def test_the_blob_is_stored_where_qgis_will_encrypt_it(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """The password field, not a name or a comment.

        QGIS encrypts the config map at rest; putting a refresh token
        anywhere QGIS treats as metadata would write it out in clear.
        """
        mod, manager = bridge
        mod.QgisAuthManagerTokenStorage("cfg-1").save(_tokens())
        stored = manager.entries["cfg-1"]
        assert "rt" in stored["password"]
        assert json.loads(stored["password"])["refresh_token"] == "rt"

    def test_saving_twice_updates_rather_than_duplicating(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """Re-sign-in reuses the id so existing layer URIs keep working."""
        from gratisgis_client.auth.tokens import TokenSet

        mod, manager = bridge
        storage = mod.QgisAuthManagerTokenStorage("cfg-1")
        storage.save(_tokens())
        storage.save(
            TokenSet(
                access_token="at2",
                refresh_token="rt2",
                access_expires_at=0.0,
                refresh_expires_at=0.0,
            )
        )
        assert len(manager.entries) == 1
        assert storage.load().refresh_token == "rt2"


class TestDegradedReads:
    def test_a_missing_entry_reads_as_signed_out(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        mod, _ = bridge
        assert mod.QgisAuthManagerTokenStorage("nope").load() is None

    def test_a_locked_auth_database_reads_as_signed_out(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """Dismissing the master-password prompt must not raise.

        It surfaces here as a failed load, and the honest answer is
        "not signed in" rather than an exception thrown into whatever
        call happened to run first.
        """
        mod, manager = bridge
        mod.QgisAuthManagerTokenStorage("cfg-1").save(_tokens())
        manager.load_ok = False
        assert mod.QgisAuthManagerTokenStorage("cfg-1").load() is None

    def test_a_corrupt_blob_reads_as_signed_out_rather_than_raising(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """An interrupted write, or an older format.

        Signing in again fixes it; a JSONDecodeError escaping into the
        Browser tree does not.
        """
        mod, manager = bridge
        manager.entries["cfg-1"] = {"password": "{not json"}
        assert mod.QgisAuthManagerTokenStorage("cfg-1").load() is None

    def test_an_empty_blob_reads_as_signed_out(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        mod, manager = bridge
        manager.entries["cfg-1"] = {"password": ""}
        assert mod.QgisAuthManagerTokenStorage("cfg-1").load() is None

    def test_a_failed_save_is_loud(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """The one operation that must NOT fail quietly.

        A silent failure here means the user signs in, sees success,
        and is signed out again next launch with no explanation.
        """
        mod, manager = bridge
        manager.store_ok = False
        with pytest.raises(RuntimeError):
            mod.QgisAuthManagerTokenStorage("cfg-1").save(_tokens())

    def test_clearing_a_missing_entry_is_not_an_error(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """Sign-out has to complete locally whatever the database thinks."""
        mod, _ = bridge
        mod.QgisAuthManagerTokenStorage("nope").clear()


class TestMakeTokenStorage:
    def test_a_profile_with_no_authcfg_gets_in_memory_storage(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        """A profile that has never signed in has no id to key on.

        In-memory keeps the client constructible so the connection can
        still be listed and edited offline.
        """
        from gratisgis_client.auth.storage import InMemoryTokenStorage

        mod, _ = bridge
        assert isinstance(mod.make_token_storage(""), InMemoryTokenStorage)
        assert isinstance(mod.make_token_storage(None), InMemoryTokenStorage)

    def test_a_real_id_gets_the_qgis_backed_storage(
        self, bridge: tuple[ModuleType, _AuthManager]
    ) -> None:
        mod, _ = bridge
        assert isinstance(
            mod.make_token_storage("cfg-1"), mod.QgisAuthManagerTokenStorage
        )
