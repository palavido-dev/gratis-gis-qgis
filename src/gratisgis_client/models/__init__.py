# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed models matching the portal-api response shapes.

The models are deliberately permissive: ``extra = "ignore"`` so the
portal can add fields without breaking older client versions. Fields
the client doesn't model yet are silently dropped on parse and
reflected in the model as missing/None on access.

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

__all__ = [
    "Item",
    "ItemList",
    "ItemSharingScope",
    "ItemSummary",
    "ItemType",
]
