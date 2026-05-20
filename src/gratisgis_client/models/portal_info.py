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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PortalApiInfo(BaseModel):
    """API location metadata."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    base_url: str = Field(alias="baseUrl")
    """Fully-qualified base URL for portal-api calls."""


class PortalAuthInfo(BaseModel):
    """Authentication backend metadata."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    type: Literal["oidc"]
    """Auth backend kind. Today only ``oidc`` exists; extending this
    union as other backends land won't break older readers because
    pydantic Literal narrows on parse."""

    issuer: str
    """OIDC issuer URL. The full discovery doc lives at
    ``{issuer}/.well-known/openid-configuration``."""


class PortalInfo(BaseModel):
    """Public discovery document for a portal.

    Fetched unauthenticated. The portal returns this from every
    deployment, regardless of org configuration; multi-tenant
    portals serve the same shape with the resolved single-tenant
    name (or the configured default) as the display string.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    """Human-readable portal name suitable for a connection list."""

    version: str
    """Portal version string mirrored from portal-api's package.json."""

    api: PortalApiInfo
    auth: PortalAuthInfo
