# SPDX-License-Identifier: AGPL-3.0-or-later
"""Portal discovery: fetch a portal's public info before sign-in.

The user supplies a portal URL. We hit ``{portal_url}/api/portal-info``
unauthenticated and parse the response into a typed ``PortalInfo``
that the caller (the QGIS plugin's connection dialog, a CLI script,
a notebook) can use to:

- show the resolved portal name in a "Connect to..." UI
- construct a ``PortalConfig`` for subsequent authenticated calls
  via ``PortalConfig.from_discovery``
- decide whether the portal version meets a minimum requirement

Discovery is intentionally a free function rather than a method on
``GratisGISClient``: at discovery time, the caller has no config yet,
so the full client lifecycle does not apply.
"""

from __future__ import annotations

import httpx

from gratisgis_client.config import PortalConfig
from gratisgis_client.errors import PortalError
from gratisgis_client.models.portal_info import PortalInfo


class PortalDiscoveryError(PortalError):
    """Discovery failed: portal unreachable, wrong URL, or non-portal
    HTTP service answering at that address.

    Carries the attempted URL so the caller can surface it in the
    "this URL doesn't look like a GratisGIS portal" error path.
    """

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


async def discover(
    portal_url: str,
    *,
    verify_tls: bool = True,
    timeout: float = 10.0,
    user_agent: str = "gratisgis-client/0.0.1.dev0",
) -> PortalInfo:
    """Fetch the discovery doc from ``{portal_url}/api/portal-info``.

    Returns a parsed ``PortalInfo``. Raises ``PortalDiscoveryError``
    when the portal does not answer, returns a non-2xx status, or
    returns a response that does not match the ``PortalInfo`` shape.

    ``verify_tls`` should stay ``True`` outside of local self-signed
    dev. ``timeout`` is the total request timeout in seconds.
    """
    base = portal_url.rstrip("/")
    url = f"{base}/api/portal-info"
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(verify=verify_tls, timeout=timeout) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise PortalDiscoveryError(
            f"Could not reach portal at {url}: {exc}", url=url
        ) from exc

    if response.status_code != 200:
        raise PortalDiscoveryError(
            f"Portal discovery returned HTTP {response.status_code}. "
            f"This does not look like a GratisGIS portal.",
            url=url,
        )

    try:
        return PortalInfo.model_validate(response.json())
    except ValueError as exc:
        # Wraps both JSONDecodeError and pydantic ValidationError;
        # both are subclasses of ValueError and the calling code does
        # not need to distinguish them.
        raise PortalDiscoveryError(
            f"Portal discovery response at {url} was not a valid PortalInfo: {exc}",
            url=url,
        ) from exc


def portal_config_from_discovery(
    *,
    portal_url: str,
    info: PortalInfo,
    client_id: str = "qgis-plugin",
    verify_tls: bool = True,
    redirect_port: int = 0,
) -> PortalConfig:
    """Construct a ``PortalConfig`` from a discovery response.

    Splits the Keycloak issuer URL
    (e.g. ``http://localhost:8080/realms/gratis-gis``) into the
    keycloak_url + realm fields that ``PortalConfig`` expects. Raises
    ``PortalDiscoveryError`` if the issuer does not have the
    ``/realms/<name>`` suffix our auth flow understands.
    """
    issuer = info.auth.issuer.rstrip("/")
    marker = "/realms/"
    idx = issuer.rfind(marker)
    if idx < 0:
        raise PortalDiscoveryError(
            f"OIDC issuer {issuer!r} is not a Keycloak realm URL. "
            f"Only Keycloak issuers are supported today.",
            url=info.auth.issuer,
        )
    keycloak_url = issuer[:idx]
    realm = issuer[idx + len(marker) :]
    if not realm or "/" in realm:
        raise PortalDiscoveryError(
            f"OIDC issuer {issuer!r} has an unexpected realm segment {realm!r}.",
            url=info.auth.issuer,
        )
    return PortalConfig(
        portal_url=portal_url,
        keycloak_url=keycloak_url,
        realm=realm,
        client_id=client_id,
        verify_tls=verify_tls,
        redirect_port=redirect_port,
    )
