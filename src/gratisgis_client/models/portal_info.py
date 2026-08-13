# SPDX-License-Identifier: AGPL-3.0-or-later
"""Portal discovery document.

Mirrors the ``PortalInfo`` interface in
``packages/shared-types/src/portal-info.ts`` in the main GratisGIS
repo. Returned by ``GET /api/portal-info`` on every portal. Lets a
fresh client bootstrap itself with just the portal URL the user
typed: name for display, OIDC issuer for sign-in, API base for
subsequent calls.

Per-client OIDC client IDs are not in the contract. Each client
hard-codes its own client_id (the QGIS plugin uses ``qgis-plugin``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from gratisgis_client._parse import req_str, require_dict


@dataclass(frozen=True, kw_only=True)
class PortalApiInfo:
    """API location metadata."""

    base_url: str
    """Fully-qualified base URL for portal-api calls."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PortalApiInfo:
        payload = require_dict(data, "PortalApiInfo")
        return cls(base_url=req_str(payload, "baseUrl"))


@dataclass(frozen=True, kw_only=True)
class PortalAuthInfo:
    """Authentication backend metadata."""

    type: Literal["oidc"]
    """Auth backend kind. Today only ``oidc`` exists; parsing stays
    strict so a future backend kind fails loudly here instead of
    producing a client that silently cannot sign in."""

    issuer: str
    """OIDC issuer URL. The full discovery doc lives at
    ``{issuer}/.well-known/openid-configuration``."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PortalAuthInfo:
        payload = require_dict(data, "PortalAuthInfo")
        kind = req_str(payload, "type")
        if kind != "oidc":
            raise ValueError(f"field 'type': unsupported auth backend {kind!r} (expected 'oidc')")
        return cls(type="oidc", issuer=req_str(payload, "issuer"))


@dataclass(frozen=True, kw_only=True)
class PortalInfo:
    """Public discovery document for a portal.

    Fetched unauthenticated. The portal returns this from every
    deployment, regardless of org configuration; multi-tenant
    portals serve the same shape with the resolved single-tenant
    name (or the configured default) as the display string.
    """

    name: str
    """Human-readable portal name suitable for a connection list."""

    version: str
    """Portal version string mirrored from portal-api's package.json."""

    api: PortalApiInfo
    auth: PortalAuthInfo

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PortalInfo:
        payload = require_dict(data, "PortalInfo")
        return cls(
            name=req_str(payload, "name"),
            version=req_str(payload, "version"),
            api=PortalApiInfo.from_api(require_dict(payload.get("api"), "field 'api'")),
            auth=PortalAuthInfo.from_api(require_dict(payload.get("auth"), "field 'auth'")),
        )
