# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection configuration for a single GratisGIS portal.

A ``PortalConfig`` is everything the client needs to know to talk to
one portal: where it lives, which Keycloak realm fronts it, which
OIDC client to authenticate as, and what redirect URI the PKCE flow
should use.

A single client instance binds to a single portal. Multi-portal
support in the QGIS plugin means maintaining multiple clients, one
per connection profile, not threading a portal id through every call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from gratisgis_client._version import __version__

DEFAULT_USER_AGENT = f"gratisgis-client/{__version__}"
"""User-Agent sent when the caller does not override it. Built from
the package version so portal access logs identify the client
release without a hand-maintained string to forget on bumps."""


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


@dataclass(frozen=True)
class PortalConfig:
    """Static configuration for one GratisGIS portal connection.

    ``portal_url`` is the base URL of the portal-web app (e.g.
    ``https://gratisgis.org``). The portal-api lives at
    ``{portal_url}/api`` by convention.

    ``keycloak_url`` is the base URL of the Keycloak server. In a
    typical deployment Keycloak sits behind the same reverse proxy
    as the portal, but it can be a separate host.

    ``realm`` identifies the Keycloak realm (default: ``gratis-gis``).

    ``client_id`` is the OIDC client. For desktop use this must be
    a public client with PKCE enabled and a localhost-loopback
    redirect URI configured. ``qgis-plugin`` is the canonical name;
    during Phase 0 development the ``field-app`` client can stand
    in because it has the right shape.

    ``redirect_port`` is the local port the PKCE loopback server
    listens on. ``0`` means pick a random free port at flow time
    (recommended). A fixed port is useful if the Keycloak client
    only allows a single redirect URI.

    ``verify_tls`` disables TLS certificate verification when set to
    ``False``. Only for local self-signed development.
    """

    portal_url: str
    keycloak_url: str
    realm: str = "gratis-gis"
    client_id: str = "qgis-plugin"
    redirect_port: int = 0
    scope: tuple[str, ...] = ("openid", "profile", "email", "offline_access")
    verify_tls: bool = True
    user_agent: str = field(default_factory=lambda: DEFAULT_USER_AGENT)

    def __post_init__(self) -> None:
        for name, value in (
            ("portal_url", self.portal_url),
            ("keycloak_url", self.keycloak_url),
        ):
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(
                    f"{name} must include http:// or https:// scheme, got {value!r}"
                )
            if not parsed.netloc:
                raise ValueError(f"{name} must include a host, got {value!r}")
        # Strip trailing slashes so endpoint join is unambiguous.
        object.__setattr__(self, "portal_url", _strip_trailing_slash(self.portal_url))
        object.__setattr__(self, "keycloak_url", _strip_trailing_slash(self.keycloak_url))

    @property
    def api_base(self) -> str:
        """Base URL of the portal-api, e.g. ``https://gratisgis.org/api``."""
        return f"{self.portal_url}/api"

    @property
    def oidc_issuer(self) -> str:
        """OIDC issuer URL for this realm.

        Keycloak's discovery doc lives at
        ``{issuer}/.well-known/openid-configuration``.
        """
        return f"{self.keycloak_url}/realms/{self.realm}"
