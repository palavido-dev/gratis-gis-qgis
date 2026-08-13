# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed models matching the portal-api response shapes.

Every model is a frozen dataclass with a ``from_api`` classmethod
that maps the portal's camelCase wire keys onto snake_case fields.
Parsing is deliberately permissive about unknown keys (``from_api``
reads only what it models) so the portal can add fields without
breaking older client versions, and strict about the keys it does
read, raising ``ValueError`` on missing or mistyped values.

Models stay in sync with ``packages/shared-types`` in the GratisGIS
monorepo; when an enum or field is added there, mirror it here.
"""

from gratisgis_client.models.item import (
    Item,
    ItemList,
    ItemSharingScope,
    ItemSummary,
    ItemType,
)
from gratisgis_client.models.portal_info import (
    PortalApiInfo,
    PortalAuthInfo,
    PortalInfo,
)

__all__ = [
    "Item",
    "ItemList",
    "ItemSharingScope",
    "ItemSummary",
    "ItemType",
    "PortalApiInfo",
    "PortalAuthInfo",
    "PortalInfo",
]
