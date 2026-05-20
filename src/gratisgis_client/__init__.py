# SPDX-License-Identifier: AGPL-3.0-or-later
"""GratisGIS portal API client.

Pure-Python async client for GratisGIS portals. No QGIS dependencies.
Used by the QGIS plugin (gratisgis_qgis) and intended for reuse in
CLI scripts, Jupyter notebooks, and automation.

Quick start:

    from gratisgis_client import GratisGISClient, PortalConfig

    config = PortalConfig(
        portal_url="https://gratisgis.org",
        keycloak_url="https://gratisgis.org",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )

    async with GratisGISClient(config) as client:
        await client.auth.login_interactive()
        items = await client.items.list()
"""

from gratisgis_client.client import GratisGISClient
from gratisgis_client.config import PortalConfig
from gratisgis_client.discovery import (
    PortalDiscoveryError,
    discover,
    portal_config_from_discovery,
)
from gratisgis_client.errors import (
    AuthError,
    ConflictError,
    GratisGISError,
    NotFoundError,
    PortalError,
    ValidationError,
)
from gratisgis_client.models.portal_info import PortalInfo

__all__ = [
    "AuthError",
    "ConflictError",
    "GratisGISClient",
    "GratisGISError",
    "NotFoundError",
    "PortalConfig",
    "PortalDiscoveryError",
    "PortalError",
    "PortalInfo",
    "ValidationError",
    "discover",
    "portal_config_from_discovery",
]

__version__ = "0.0.1.dev0"
