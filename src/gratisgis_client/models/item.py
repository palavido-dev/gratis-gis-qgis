# SPDX-License-Identifier: AGPL-3.0-or-later
"""Item models: the base unit of the portal.

A portal ``item`` is every addressable thing: maps, data layers,
forms, dashboards, files. ``ItemType`` is the enum of valid types
mirroring ``packages/shared-types`` in the GratisGIS monorepo.

The model intentionally exposes ``data`` as ``dict`` rather than a
typed union, because the shape of ``data`` depends on ``type``. The
endpoint module's helpers (``items.get_data_layer``,
``items.get_map``, ...) narrow it to a typed model for the common
item types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Free-form string rather than a closed Literal union. The portal
# has grown its item-type vocabulary faster than this client could
# track (kebab-case 'data-layer', 'web-app', 'pick-list',
# 'geo-boundary' plus newer 'basemap', 'service', 'theme',
# 'app-template', 'print-template', 'geocoding-service' on prod).
# A Literal here would reject every list / search response that
# contains an unrecognized type and stall the whole plugin.
#
# KNOWN_ITEM_TYPES below stays as a documentation aide and powers
# the search dock's filter dropdown; anything not in the list still
# round-trips through the API just fine and renders with a generic
# icon in the Browser tree.
ItemType = str

KNOWN_ITEM_TYPES: tuple[str, ...] = (
    "map",
    "data-layer",
    "data_layer",
    "arcgis_service",
    "service",
    "form",
    "form_submission_collection",
    "web-app",
    "web_app",
    "report_template",
    "print-template",
    "dashboard",
    "file",
    "layer_package",
    "tool",
    "widget_package",
    "pick-list",
    "pick_list",
    "geo-boundary",
    "geo_boundary",
    "folder",
    "tile_layer",
    "basemap",
    "theme",
    "app-template",
    "geocoding-service",
)
"""Best-effort enumeration of item types the portal is known to
emit, accounting for both the kebab-case and snake_case spellings
the schema has used over time. New types simply land on the API
side without requiring a plugin release."""


ItemSharingScope = Literal["private", "org", "public"]


class ItemSummary(BaseModel):
    """The slim version of an item, as returned by ``GET /api/items``
    list responses.

    Carries enough to render a Browser tree row: id, title, type,
    sharing scope, modification timestamps, tags, thumbnail URL.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    type: ItemType
    title: str
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    access: ItemSharingScope
    owner_id: str = Field(alias="ownerId")
    owner_username: str | None = Field(default=None, alias="ownerUsername")
    org_id: str = Field(alias="orgId")
    folder_id: str | None = Field(default=None, alias="folderId")
    thumbnail_url: str | None = Field(default=None, alias="thumbnailUrl")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class Item(ItemSummary):
    """The full version of an item, as returned by ``GET /api/items/:id``.

    Extends ``ItemSummary`` with the ``data`` envelope (type-specific
    payload), license, and thumbnail design.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    data: dict[str, Any] = Field(default_factory=dict)
    license: str | None = None
    thumbnail_design: dict[str, Any] | None = Field(default=None, alias="thumbnailDesign")


class ItemList(BaseModel):
    """Paginated list response for ``GET /api/items``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    items: list[ItemSummary]
    total: int | None = None
    next_cursor: str | None = Field(default=None, alias="nextCursor")
