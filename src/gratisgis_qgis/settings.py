# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection profile management.

A "connection" is one configured GratisGIS portal: a friendly name,
its base URL, Keycloak realm and client id, and a pointer (via the
QGIS auth manager) at the encrypted token store for that connection.

Connection profiles live in QSettings under
``GratisGIS/connections/<name>/*``. Tokens live in the QGIS auth
manager keyed by a per-connection authcfg id stored alongside the
profile.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from qgis.PyQt.QtCore import QSettings  # type: ignore[import-not-found]

from gratisgis_client.config import PortalConfig

SETTINGS_ROOT = "GratisGIS/connections"


@dataclass(frozen=True)
class ConnectionProfile:
    """A user-managed entry in the connection list.

    ``name`` is the unique key (also displayed as the Browser tree
    root label). ``authcfg_id`` is the QGIS auth manager id; if blank,
    the profile is configured but the user hasn't signed in yet.
    """

    name: str
    portal_url: str
    keycloak_url: str
    realm: str
    client_id: str
    authcfg_id: str = ""
    verify_tls: bool = True

    def to_portal_config(self) -> PortalConfig:
        return PortalConfig(
            portal_url=self.portal_url,
            keycloak_url=self.keycloak_url,
            realm=self.realm,
            client_id=self.client_id,
            verify_tls=self.verify_tls,
        )


class ConnectionStore:
    """Read/write connection profiles through QSettings.

    All operations are synchronous (QSettings is fast and local) and
    return plain ``ConnectionProfile`` instances. The class never
    touches the auth manager directly; that's
    ``gratisgis_qgis.auth_bridge``.
    """

    def __init__(self, qsettings: QSettings | None = None) -> None:
        self._s = qsettings if qsettings is not None else QSettings()

    def list_names(self) -> list[str]:
        self._s.beginGroup(SETTINGS_ROOT)
        names = sorted(self._s.childGroups())
        self._s.endGroup()
        return list(names)

    def get(self, name: str) -> ConnectionProfile | None:
        prefix = f"{SETTINGS_ROOT}/{name}"
        portal_url = self._s.value(f"{prefix}/portal_url", "", type=str)
        if not portal_url:
            return None
        return ConnectionProfile(
            name=name,
            portal_url=portal_url,
            keycloak_url=self._s.value(f"{prefix}/keycloak_url", "", type=str),
            realm=self._s.value(f"{prefix}/realm", "gratis-gis", type=str),
            client_id=self._s.value(f"{prefix}/client_id", "qgis-plugin", type=str),
            authcfg_id=self._s.value(f"{prefix}/authcfg_id", "", type=str),
            verify_tls=self._s.value(f"{prefix}/verify_tls", True, type=bool),
        )

    def save(self, profile: ConnectionProfile) -> None:
        prefix = f"{SETTINGS_ROOT}/{profile.name}"
        self._s.setValue(f"{prefix}/portal_url", profile.portal_url)
        self._s.setValue(f"{prefix}/keycloak_url", profile.keycloak_url)
        self._s.setValue(f"{prefix}/realm", profile.realm)
        self._s.setValue(f"{prefix}/client_id", profile.client_id)
        self._s.setValue(f"{prefix}/authcfg_id", profile.authcfg_id)
        self._s.setValue(f"{prefix}/verify_tls", profile.verify_tls)

    def delete(self, name: str) -> None:
        self._s.remove(f"{SETTINGS_ROOT}/{name}")

    @staticmethod
    def new_authcfg_id() -> str:
        """Produce a fresh QGIS auth manager id.

        QGIS uses 7-char alphanumeric ids; using a UUID prefix is
        plenty unique without colliding with anything QGIS itself
        generates.
        """
        return uuid.uuid4().hex[:7]
