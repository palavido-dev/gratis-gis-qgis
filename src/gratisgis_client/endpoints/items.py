# SPDX-License-Identifier: AGPL-3.0-or-later
"""Items endpoint: list, get, create, update, delete, sharing."""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

from gratisgis_client.models.item import (
    Item,
    ItemList,
    ItemSharingScope,
    ItemSummary,
    ItemType,
)

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


class ItemsEndpoint:
    """Wrapper over ``/api/items`` and related routes.

    All methods block on the network. The endpoint does not cache;
    that's the plugin's responsibility (Layer 5 in the architecture).
    """

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    def list(
        self,
        *,
        types: list[ItemType] | None = None,
        access: ItemSharingScope | None = None,
        folder_id: str | None = None,
        owner_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ItemList:
        """List items the caller can see.

        Server applies sharing rules automatically; this returns the
        union of (private items you own) + (org items in your org) +
        (public items) intersected with type filters.
        """
        params: dict[str, Any] = {"limit": limit}
        if types:
            params["type"] = list(types)
        if access:
            params["access"] = access
        if folder_id:
            params["folderId"] = folder_id
        if owner_id:
            params["ownerId"] = owner_id
        if query:
            params["q"] = query
        if cursor:
            params["cursor"] = cursor
        body = self._http.request_json("GET", "/items", params=params)
        # Portal returns either a bare array or a paginated object.
        # Normalize both into ItemList so callers don't have to care.
        if isinstance(body, list):
            return ItemList(items=[ItemSummary.from_api(it) for it in body])
        return ItemList.from_api(body)

    def get(self, item_id: str) -> Item:
        """Fetch a single item envelope by id."""
        body = self._http.request_json("GET", f"/items/{item_id}")
        return Item.from_api(body)

    def create(
        self,
        *,
        type: ItemType,
        title: str,
        data: dict[str, Any],
        description: str | None = None,
        # builtins.list because the surrounding class shadows `list`
        # with a method of that name; the shadowing only affects
        # annotations parsed after the method definition.
        tags: builtins.list[str] | None = None,
        access: ItemSharingScope = "private",
    ) -> Item:
        """Create a new item.

        ``data`` is the type-specific payload. For ``data_layer``,
        that's the multi-layer schema envelope; for ``map``, the
        web-map JSON; and so on. Endpoint-level helpers for the
        common types land in Phase 1.
        """
        body = self._http.request_json(
            "POST",
            "/items",
            json={
                "type": type,
                "title": title,
                "data": data,
                **({"description": description} if description is not None else {}),
                **({"tags": tags} if tags is not None else {}),
                "access": access,
            },
        )
        return Item.from_api(body)

    def update(
        self,
        item_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
        data: dict[str, Any] | None = None,
        access: ItemSharingScope | None = None,
    ) -> Item:
        """Partial update of item fields."""
        patch: dict[str, Any] = {}
        if title is not None:
            patch["title"] = title
        if description is not None:
            patch["description"] = description
        if tags is not None:
            patch["tags"] = tags
        if data is not None:
            patch["data"] = data
        if access is not None:
            patch["access"] = access
        body = self._http.request_json("PATCH", f"/items/{item_id}", json=patch)
        return Item.from_api(body)

    def list_group_shares(self, item_id: str) -> builtins.list[str]:
        """Group ids this item is currently shared with.

        Read off the full item payload, which carries every share row;
        user-principal shares are filtered out here because the plugin
        surface (today) manages group shares only.
        """
        body = self._http.request_json("GET", f"/items/{item_id}")
        payload = body if isinstance(body, dict) else {}
        shares = payload.get("shares")
        out: builtins.list[str] = []
        for share in shares if isinstance(shares, list) else []:
            if not isinstance(share, dict):
                continue
            if share.get("principalType") != "group":
                continue
            principal = str(share.get("principalId") or "")
            if principal and principal not in out:
                out.append(principal)
        return out

    def share_with_group(
        self, item_id: str, group_id: str, *, permission: str = "view"
    ) -> None:
        """Grant a group access to an item.

        The portal upserts, so re-sharing an already-shared group is
        safe, and the portal is the enforcer of who may share at all.
        """
        self._http.request_json(
            "POST",
            f"/items/{item_id}/share",
            json={
                "principalType": "group",
                "principalId": group_id,
                "permission": permission,
            },
        )

    def unshare_with_group(self, item_id: str, group_id: str) -> None:
        self._http.request_json(
            "DELETE",
            f"/items/{item_id}/share",
            json={"principalType": "group", "principalId": group_id},
        )

    def delete(self, item_id: str) -> None:
        """Soft-delete an item (moves to trash, can be restored)."""
        self._http.request_json("DELETE", f"/items/{item_id}")
