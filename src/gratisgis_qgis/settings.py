# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection profile management.

A "connection" is one configured GratisGIS portal. The user only
ever types the portal URL; everything else (the portal's display
name, version, API base URL, OIDC issuer) is fetched via the
portal-info discovery endpoint on first save and cached on the
profile so that signed-out QGIS sessions can still list and edit
profiles offline.

Connection profiles live in QSettings under
``GratisGIS/connections/<name>/*``. Tokens live in the QGIS auth
manager keyed by a per-connection authcfg id stored alongside the
profile.

Backward compatibility: pre-discovery profiles wrote ``keycloak_url``
and ``realm`` directly. On load we synthesize ``oidc_issuer`` from
those legacy fields so existing connections keep working through
the format migration. Once a legacy profile is saved again it gets
written in the new shape; the legacy keys are left in QSettings as
harmless orphans (cleanup is cheap to add later if it ever matters).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from qgis.PyQt.QtCore import QSettings  # type: ignore[import-not-found]

from gratisgis_client.config import PortalConfig
from gratisgis_client.discovery import portal_config_from_discovery
from gratisgis_client.models.portal_info import (
    PortalApiInfo,
    PortalAuthInfo,
    PortalInfo,
)

SETTINGS_ROOT = "GratisGIS/connections"

# Hard-coded for the QGIS plugin; the portal's discovery doc
# intentionally does not return per-client IDs so siblings cannot
# learn each other's. See packages/shared-types/portal-info.ts.
QGIS_PLUGIN_CLIENT_ID = "qgis-plugin"


@dataclass(frozen=True)
class ConnectionProfile:
    """A user-managed entry in the connection list.

    ``name`` is the QSettings key (and the label shown in the
    connection list). ``portal_url`` is the only field the user
    enters; everything below is populated from portal-info discovery.

    ``authcfg_id`` is the QGIS auth manager id; if blank, the
    profile has no signed-in session yet.
    """

    name: str
    portal_url: str
    verify_tls: bool = True
    authcfg_id: str = ""
    # Cached portal-info fields. All empty for a freshly created
    # profile until discovery runs at sign-in time.
    portal_name: str = ""
    portal_version: str = ""
    api_base_url: str = ""
    oidc_issuer: str = ""
    discovered_at: float = 0.0

    @property
    def is_discovered(self) -> bool:
        """True once discovery has populated the cached fields.

        ``to_portal_config`` requires this. The dialog gates sign-in
        on discovery, so signed-in profiles always have it set.
        """
        return bool(self.oidc_issuer)

    @property
    def display_label(self) -> str:
        """User-facing label, prefers the portal's own name over the
        local profile key. Falls back to the QSettings key for fresh
        profiles where discovery hasn't run yet.
        """
        return self.portal_name or self.name

    def to_portal_config(self) -> PortalConfig:
        """Construct a ``PortalConfig`` from the cached discovery.

        Raises ``ValueError`` if discovery has not run yet; callers
        should run ``discover()`` and produce an updated profile via
        ``with_discovery`` first.
        """
        if not self.is_discovered:
            raise ValueError(
                f"Connection profile {self.name!r} has not been discovered yet."
            )
        info = PortalInfo(
            name=self.portal_name,
            version=self.portal_version,
            api=PortalApiInfo(base_url=self.api_base_url),
            auth=PortalAuthInfo(type="oidc", issuer=self.oidc_issuer),
        )
        return portal_config_from_discovery(
            portal_url=self.portal_url,
            info=info,
            client_id=QGIS_PLUGIN_CLIENT_ID,
            verify_tls=self.verify_tls,
        )

    def with_discovery(self, info: PortalInfo, *, now: float) -> ConnectionProfile:
        """Return a copy with the discovery fields populated."""
        return ConnectionProfile(
            name=self.name,
            portal_url=self.portal_url,
            verify_tls=self.verify_tls,
            authcfg_id=self.authcfg_id,
            portal_name=info.name,
            portal_version=info.version,
            api_base_url=info.api.base_url,
            oidc_issuer=info.auth.issuer,
            discovered_at=now,
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

        # New-shape field; empty for legacy profiles or freshly
        # created ones that haven't been discovered yet.
        oidc_issuer = self._s.value(f"{prefix}/oidc_issuer", "", type=str)

        # Backward compatibility: legacy profiles stored keycloak_url
        # and realm as separate keys. Synthesize the issuer URL so the
        # connection still works without forcing the user to re-add it.
        if not oidc_issuer:
            keycloak_url = self._s.value(f"{prefix}/keycloak_url", "", type=str)
            realm = self._s.value(f"{prefix}/realm", "gratis-gis", type=str)
            if keycloak_url:
                oidc_issuer = f"{keycloak_url.rstrip('/')}/realms/{realm}"

        return ConnectionProfile(
            name=name,
            portal_url=portal_url,
            verify_tls=self._s.value(f"{prefix}/verify_tls", True, type=bool),
            authcfg_id=self._s.value(f"{prefix}/authcfg_id", "", type=str),
            portal_name=self._s.value(f"{prefix}/portal_name", "", type=str),
            portal_version=self._s.value(f"{prefix}/portal_version", "", type=str),
            api_base_url=self._s.value(f"{prefix}/api_base_url", "", type=str),
            oidc_issuer=oidc_issuer,
            discovered_at=self._s.value(f"{prefix}/discovered_at", 0.0, type=float),
        )

    def save(self, profile: ConnectionProfile) -> None:
        prefix = f"{SETTINGS_ROOT}/{profile.name}"
        self._s.setValue(f"{prefix}/portal_url", profile.portal_url)
        self._s.setValue(f"{prefix}/verify_tls", profile.verify_tls)
        self._s.setValue(f"{prefix}/authcfg_id", profile.authcfg_id)
        self._s.setValue(f"{prefix}/portal_name", profile.portal_name)
        self._s.setValue(f"{prefix}/portal_version", profile.portal_version)
        self._s.setValue(f"{prefix}/api_base_url", profile.api_base_url)
        self._s.setValue(f"{prefix}/oidc_issuer", profile.oidc_issuer)
        self._s.setValue(f"{prefix}/discovered_at", profile.discovered_at)

    def delete(self, name: str) -> None:
        self._s.remove(f"{SETTINGS_ROOT}/{name}")

    def unique_name(self, suggested: str) -> str:
        """Return a connection name not in use yet.

        If ``suggested`` is free, return it as-is. Otherwise append
        ``" (2)"``, ``" (3)"``, ... until one is free.
        """
        existing = set(self.list_names())
        if suggested not in existing:
            return suggested
        n = 2
        while f"{suggested} ({n})" in existing:
            n += 1
        return f"{suggested} ({n})"

    @staticmethod
    def new_authcfg_id() -> str:
        """Produce a fresh QGIS auth manager id.

        QGIS uses 7-char alphanumeric ids; using a UUID prefix is
        plenty unique without colliding with anything QGIS itself
        generates.
        """
        return uuid.uuid4().hex[:7]
