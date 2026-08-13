# SPDX-License-Identifier: AGPL-3.0-or-later
"""GratisGIS portal API client.

Synchronous, dependency-free Python client for GratisGIS portals.
Stdlib only, so it vendors cleanly into a stock QGIS install (which
ships no third-party wheels). No QGIS dependencies here either; the
QGIS plugin (gratisgis_qgis) layers threading and UI on top, and the
same package works from CLI scripts and notebooks.

For a standalone pip-installable SDK, use the ``gratisgis`` package
from the main GratisGIS repo instead; this client exists to serve
the plugin.

Quick start:

    from gratisgis_client import GratisGISClient, PortalConfig

    config = PortalConfig(
        portal_url="https://gratisgis.org",
        keycloak_url="https://gratisgis.org",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )

    with GratisGISClient(config) as client:
        client.auth.login_interactive()
        items = client.items.list()
"""

from gratisgis_client._version import __version__ as __version__
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
