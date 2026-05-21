# SPDX-License-Identifier: AGPL-3.0-or-later
"""GratisGISClient: top-level client tying everything together.

Usage:

    from gratisgis_client import GratisGISClient, PortalConfig

    config = PortalConfig(
        portal_url="https://gratisgis.org",
        keycloak_url="https://gratisgis.org",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )

    async with GratisGISClient(config) as client:
        if not await client.auth.is_signed_in():
            await client.auth.login_interactive()
        items = await client.items.list()
        for item in items.items:
            print(item.id, item.type, item.title)
"""

from __future__ import annotations

from types import TracebackType

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.auth.storage import TokenStorage
from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.import_jobs import ImportJobsEndpoint
from gratisgis_client.endpoints.ingest import IngestEndpoint
from gratisgis_client.endpoints.items import ItemsEndpoint
from gratisgis_client.http import PortalHttp


class GratisGISClient:
    """One client per portal connection.

    The client owns an ``AuthManager`` and a ``PortalHttp``. Endpoint
    groupings (``client.items``, etc.) are lightweight wrappers that
    share the single ``PortalHttp``.

    Use as an async context manager so the underlying HTTP clients
    get closed cleanly:

        async with GratisGISClient(config) as client:
            ...
    """

    def __init__(
        self,
        config: PortalConfig,
        *,
        token_storage: TokenStorage | None = None,
    ) -> None:
        self._config = config
        self.auth = AuthManager(config, storage=token_storage)
        self._http = PortalHttp(config, self.auth)
        self.items = ItemsEndpoint(self._http)
        self.ingest = IngestEndpoint(self._http)
        self.import_jobs = ImportJobsEndpoint(self._http)

    @property
    def config(self) -> PortalConfig:
        return self._config

    async def close(self) -> None:
        await self._http.close()
        await self.auth.close()

    async def __aenter__(self) -> GratisGISClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
