# SPDX-License-Identifier: AGPL-3.0-or-later
"""Thread-safe bridge between QGIS's sync Browser tree and our
async portal client.

QGIS calls `QgsDataCollectionItem.createChildren()` on the worker
thread that drives the Browser panel, so each call here happens
off the UI thread. The portal client is async (httpx + asyncio);
we wrap each top-level fetch in `asyncio.run()` on a fresh event
loop so the call stays self-contained and doesn't tangle with any
loop QGIS may already own (it doesn't, but this stays safe even
if a future Qt build adds one).

For Phase 1 the fetch surface is small:
    - `list_items(profile, types=None, query=None)` -> list of
      Item summaries the signed-in user can read.
    - `get_item(profile, item_id)` -> single Item envelope.

Token retrieval routes through `auth_bridge.make_token_storage`
so the auth manager remains the source of truth for credentials.
"""
from __future__ import annotations

import asyncio
from typing import Any

from gratisgis_client.client import GratisGISClient
from gratisgis_client.models.item import ItemSummary, ItemType

from ..auth_bridge import make_token_storage
from ..log import get_logger
from ..settings import ConnectionProfile

_log = get_logger(__name__)


def list_items_sync(
    profile: ConnectionProfile,
    *,
    types: list[ItemType] | None = None,
    query: str | None = None,
    owner_id: str | None = None,
    limit: int = 200,
) -> list[ItemSummary]:
    """Blocking wrapper that returns the items the signed-in user
    can see for the given connection. Returns an empty list when
    the profile isn't signed in yet; callers shouldn't 500 the
    Browser tree on a fresh / unauthenticated profile.
    """
    if not profile.is_discovered or not profile.authcfg_id:
        _log.debug("list_items_sync: profile not discovered or not signed in: %s", profile.name)
        return []
    return _run(_list_items(profile, types=types, query=query, owner_id=owner_id, limit=limit))


def get_item_sync(profile: ConnectionProfile, item_id: str) -> dict[str, Any] | None:
    """Blocking fetch of a single item's full envelope. None on
    error rather than raising, since the Browser tree path
    shouldn't blow up on a missing item.
    """
    if not profile.is_discovered or not profile.authcfg_id:
        return None
    try:
        return _run(_get_item(profile, item_id))
    except Exception:  # pragma: no cover - defensive
        _log.exception("get_item_sync failed for %s", item_id)
        return None


# -----------------------------------------------------------
# Internal async helpers
# -----------------------------------------------------------


async def _list_items(
    profile: ConnectionProfile,
    *,
    types: list[ItemType] | None,
    query: str | None,
    owner_id: str | None,
    limit: int,
) -> list[ItemSummary]:
    async with _connected_client(profile) as client:
        out = await client.items.list(
            types=types,
            query=query,
            owner_id=owner_id,
            limit=limit,
        )
        return list(out.items)


async def _get_item(profile: ConnectionProfile, item_id: str) -> dict[str, Any]:
    async with _connected_client(profile) as client:
        item = await client.items.get(item_id)
        return item.model_dump(mode="json", by_alias=True)


def _run(coro):
    """Run `coro` to completion on a fresh event loop.

    `asyncio.run()` creates and tears down its own loop, so each
    Browser-thread fetch is hermetic. Important: never call this
    from a thread that already has a running loop -- in QGIS the
    Browser worker doesn't, but a future caller might.
    """
    return asyncio.run(coro)


def _connected_client(profile: ConnectionProfile):
    """Build a `GratisGISClient` bound to this profile's auth.

    Returns the async context manager so callers can `async with`.
    Token storage comes from the QGIS auth manager via the
    existing bridge so signed-in profiles reuse their refresh
    tokens transparently.
    """
    config = profile.to_portal_config()
    storage = make_token_storage(profile.authcfg_id)
    return GratisGISClient(config=config, token_storage=storage)
