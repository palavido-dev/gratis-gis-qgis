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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from gratisgis_client._parse import (
    opt_dict,
    opt_int,
    opt_str,
    req_datetime,
    req_str,
    require_dict,
    str_list,
)

# Free-form string rather than a closed Literal union. The portal
# adds new item types over time (per packages/shared-types/src/
# item-types.ts in the main gratis-gis repo). A Literal here
# would reject any list / search response carrying an
# unrecognized type and stall the whole plugin.
#
# KNOWN_ITEM_TYPES below mirrors the authoritative ITEM_TYPES
# constant in shared-types one-for-one, snake_case only (per the
# Prisma schema: the on-disk column uses @map("kebab-case") but
# the API serializes the enum's TypeScript name, which is
# snake_case). It powers the search dock's filter dropdown and is
# the canonical "things the plugin knows the portal can emit"
# list. Anything outside the list still round-trips fine and
# renders with a generic icon.
ItemType = str

KNOWN_ITEM_TYPES: tuple[str, ...] = (
    "map",
    "data_layer",
    "derived_layer",
    "arcgis_service",
    "form",
    "form_submission_collection",
    "web_app",
    "report_template",
    "dashboard",
    "file",
    "layer_package",
    "tool",
    "widget_package",
    "pick_list",
    "geo_boundary",
    "basemap",
    "wms_service",
    "wfs_service",
    # #304 unified connected-service item type (replaces the four
    # protocol-specific *_service entries above, which stay
    # listed for the deprecation window).
    "service",
    "folder",
    "editor",
    "data_collection",
    "geocoding_service",
    "tile_layer",
    "app_template",
    "theme",
    "print_template",
    # #179 point clouds served as ordinary layers (COPC-backed).
    "point_cloud",
    # #221 server-side Python scripts stored as items.
    "script",
)
"""Mirrors `ITEM_TYPES` in packages/shared-types/src/item-types.ts
exactly. When a new type lands on the portal, add it here too so
the search dock's filter dropdown surfaces it."""


ItemSharingScope = Literal["private", "org", "public"]

_SHARING_SCOPES: tuple[ItemSharingScope, ...] = ("private", "org", "public")


def _sharing_scope(data: dict[str, Any]) -> ItemSharingScope:
    value = req_str(data, "access")
    if value not in _SHARING_SCOPES:
        raise ValueError(
            f"field 'access': expected one of {', '.join(_SHARING_SCOPES)}, got {value!r}"
        )
    return value


@dataclass(frozen=True, kw_only=True)
class ItemSummary:
    """The slim version of an item, as returned by ``GET /api/items``
    list responses.

    Carries enough to render a Browser tree row: id, title, type,
    sharing scope, modification timestamps, tags, thumbnail URL.
    """

    id: str
    type: ItemType
    title: str
    summary: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    access: ItemSharingScope
    owner_id: str
    #: Human-readable owner, lifted out of the portal's nested `owner`
    #: object. The portal has never sent a flat `ownerUsername`, so
    #: anything reading that alone fell back to showing a raw UUID.
    owner_username: str | None = None
    owner_full_name: str | None = None
    org_id: str
    folder_id: str | None = None
    thumbnail_url: str | None = None
    #: Geographic extent as (min_lon, min_lat, max_lon, max_lat) in
    #: EPSG:4326, which is the CRS the portal states in ``bboxSrs``.
    #: None when the portal has no extent for the item, which it sends
    #: as an empty array rather than null. Kept in 4326 rather than the
    #: layer's own CRS so this module needs no coordinate machinery;
    #: the QGIS side transforms when it applies the extent.
    bbox: tuple[float, float, float, float] | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ItemSummary:
        return cls(**_summary_kwargs(require_dict(data, "ItemSummary")))

    def to_api_dict(self) -> dict[str, Any]:
        """The wire shape back, camelCase keys, JSON-safe values.

        Round-trips with ``from_api`` so callers can stash a summary
        in a Qt item role (or any JSON sink) and re-hydrate it later.
        """
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "summary": self.summary,
            "description": self.description,
            "tags": list(self.tags),
            "access": self.access,
            "ownerId": self.owner_id,
            # Emit the nested shape the portal itself uses so a
            # round-tripped payload still parses through from_api.
            "owner": {
                "id": self.owner_id,
                "username": self.owner_username,
                "fullName": self.owner_full_name,
            },
            "orgId": self.org_id,
            "folderId": self.folder_id,
            "thumbnailUrl": self.thumbnail_url,
            # Empty array, not null, matching what the portal emits for
            # an item with no extent, so a round-tripped payload is
            # indistinguishable from a fetched one.
            "bbox": list(self.bbox) if self.bbox is not None else [],
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
        }


def _owner_field(data: dict[str, Any], key: str) -> str | None:
    """Read one field out of the portal's nested ``owner`` object.

    The portal ships ``owner: {id, username, fullName, avatarUrl}`` on
    both the list and the single-item read. It has never emitted a flat
    ``ownerUsername``, so the flat lookup this replaced always missed
    and every owner rendered as a bare UUID. The flat spelling is still
    accepted as a fallback in case a future payload adds it.
    """
    owner = data.get("owner")
    if isinstance(owner, dict):
        value = owner.get(key)
        if isinstance(value, str) and value:
            return value
    flat = data.get(f"owner{key[0].upper()}{key[1:]}")
    return flat if isinstance(flat, str) and flat else None


def _summary_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    """Parsed constructor kwargs for the ``ItemSummary`` fields.

    Shared between ``ItemSummary.from_api`` and ``Item.from_api`` so
    the alias map exists exactly once.
    """
    return {
        "id": req_str(data, "id"),
        "type": req_str(data, "type"),
        "title": req_str(data, "title"),
        "summary": opt_str(data, "summary"),
        "description": opt_str(data, "description"),
        "tags": str_list(data, "tags"),
        "access": _sharing_scope(data),
        "owner_id": req_str(data, "ownerId"),
        "owner_username": _owner_field(data, "username"),
        "owner_full_name": _owner_field(data, "fullName"),
        "org_id": req_str(data, "orgId"),
        "folder_id": opt_str(data, "folderId"),
        "thumbnail_url": opt_str(data, "thumbnailUrl"),
        "bbox": _bbox(data),
        "created_at": req_datetime(data, "createdAt"),
        "updated_at": req_datetime(data, "updatedAt"),
    }


def _bbox(data: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Parse the portal's ``bbox`` array, tolerantly.

    The portal sends ``[]`` for an item it has no extent for (tile
    layers today), so an empty array means "unknown", not "empty
    region". Anything that is not four finite numbers is treated the
    same way: an extent is a convenience for zooming, and refusing to
    parse an item because its bbox is odd would be a poor trade.

    Degenerate extents (a single point, or a zero-width axis) are
    returned as-is. The caller knows how to pad one; this layer should
    not invent geography.
    """
    raw = data.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    values: list[float] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            return None
        number = float(entry)
        # NaN and infinities would poison every later comparison.
        if number != number or number in (float("inf"), float("-inf")):
            return None
        values.append(number)
    min_lon, min_lat, max_lon, max_lat = values
    if min_lon > max_lon or min_lat > max_lat:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


@dataclass(frozen=True, kw_only=True)
class Item(ItemSummary):
    """The full version of an item, as returned by ``GET /api/items/:id``.

    Extends ``ItemSummary`` with the ``data`` envelope (type-specific
    payload), license, and thumbnail design.
    """

    data: dict[str, Any] = field(default_factory=dict)
    license: str | None = None
    thumbnail_design: dict[str, Any] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Item:
        payload = require_dict(data, "Item")
        kwargs = _summary_kwargs(payload)
        envelope = payload.get("data")
        kwargs["data"] = require_dict(envelope, "field 'data'") if envelope is not None else {}
        kwargs["license"] = opt_str(payload, "license")
        kwargs["thumbnail_design"] = opt_dict(payload, "thumbnailDesign")
        return cls(**kwargs)

    def to_api_dict(self) -> dict[str, Any]:
        out = super().to_api_dict()
        out["data"] = dict(self.data)
        out["license"] = self.license
        out["thumbnailDesign"] = (
            dict(self.thumbnail_design) if self.thumbnail_design is not None else None
        )
        return out


@dataclass(frozen=True, kw_only=True)
class ItemList:
    """Paginated list response for ``GET /api/items``."""

    items: list[ItemSummary]
    total: int | None = None
    next_cursor: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ItemList:
        payload = require_dict(data, "ItemList")
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise ValueError("field 'items': expected a list")
        return cls(
            items=[ItemSummary.from_api(row) for row in rows],
            total=opt_int(payload, "total"),
            next_cursor=opt_str(payload, "nextCursor"),
        )
