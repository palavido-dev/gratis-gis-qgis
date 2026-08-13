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

    with GratisGISClient(config) as client:
        if not client.auth.is_signed_in():
            client.auth.login_interactive()
        items = client.items.list()
        for item in items.items:
            print(item.id, item.type, item.title)
"""

from __future__ import annotations

from types import TracebackType

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.auth.storage import TokenStorage
from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.api_keys import ApiKeysEndpoint
from gratisgis_client.endpoints.features import FeaturesEndpoint
from gratisgis_client.endpoints.import_jobs import ImportJobsEndpoint
from gratisgis_client.endpoints.ingest import IngestEndpoint
from gratisgis_client.endpoints.items import ItemsEndpoint
from gratisgis_client.endpoints.storage import StorageEndpoint
from gratisgis_client.endpoints.tile_layer import TileLayerEndpoint
from gratisgis_client.http import PortalHttp
from gratisgis_client.transport import Transport, UrllibTransport


class GratisGISClient:
    """One client per portal connection.

    The client owns an ``AuthManager`` and a ``PortalHttp``, both
    sharing one ``Transport``. Endpoint groupings (``client.items``,
    etc.) are lightweight wrappers that share the single
    ``PortalHttp``.

    Usable as a context manager for symmetry with resource-owning
    clients, though the stdlib transport holds nothing open between
    requests:

        with GratisGISClient(config) as client:
            ...
    """

    def __init__(
        self,
        config: PortalConfig,
        token_storage: TokenStorage | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._config = config
        shared_transport: Transport = (
            transport
            if transport is not None
            else UrllibTransport(verify_tls=config.verify_tls)
        )
        self.auth = AuthManager(config, storage=token_storage, transport=shared_transport)
        self._http = PortalHttp(config, self.auth, transport=shared_transport)
        self.items = ItemsEndpoint(self._http)
        self.ingest = IngestEndpoint(self._http)
        self.import_jobs = ImportJobsEndpoint(self._http)
        self.features = FeaturesEndpoint(self._http)
        self.storage = StorageEndpoint(self._http)
        self.tile_layer = TileLayerEndpoint(self._http)
        self.api_keys = ApiKeysEndpoint(self._http)

    @property
    def config(self) -> PortalConfig:
        return self._config

    def close(self) -> None:
        self._http.close()
        self.auth.close()

    def __enter__(self) -> GratisGISClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
