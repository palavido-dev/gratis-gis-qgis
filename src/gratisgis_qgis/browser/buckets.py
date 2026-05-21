# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-Python bucket filtering for the Browser tree.

Kept separate from `items.py` so the filtering logic can be tested
without requiring the QGIS runtime to import. `items.py` imports
the helpers from here.

A "bucket" is one of the four scope-discriminator nodes that
appear under each connection (My Content / Shared / Org / Public).
The portal's `/api/items` list endpoint already applies sharing
rules server-side; this module only re-slices the result into the
four buckets the user sees, based on access + ownership.
"""
from __future__ import annotations

from collections.abc import Iterable

from gratisgis_client.models.item import ItemSummary


class BucketKind:
    """String constants for the four bucket discriminators.

    Public for stable matching from the items.py + tests; the
    labels themselves live in `items.py` as a separate mapping
    (we may want to swap them for translated strings later).
    """

    MINE = "mine"
    SHARED = "shared"
    ORG = "org"
    PUBLIC = "public"


_ALL = (BucketKind.MINE, BucketKind.SHARED, BucketKind.ORG, BucketKind.PUBLIC)


def all_buckets() -> tuple[str, ...]:
    """The four bucket discriminators in display order."""
    return _ALL


def filter_for_bucket(
    items: Iterable[ItemSummary], kind: str
) -> list[ItemSummary]:
    """Slice the full items list down to one bucket's view.

    Args:
        items: every item the signed-in caller can see, as
            returned by /api/items.
        kind: one of `BucketKind.*`.

    Returns:
        Items that belong in that bucket, in input order.

    Semantics:
      - **public**: items with access=public.
      - **mine**: items the caller owns. Inferred via the most
        common owner_id across the result's private items
        (the portal-side share filter already removed items the
        caller can't read, so a `private` item in the result is
        necessarily owned by the caller). Falls back to an
        empty list when no private items are present so we don't
        misattribute an org-only roster.
      - **shared**: items with access=org NOT owned by the caller
        (approximation until the /me echo or shares roster is
        exposed on the list payload).
      - **org**: items with access in {org, public} -- the union
        of "anyone in my org can read this".
    """
    items_list = list(items)
    if kind == BucketKind.PUBLIC:
        return [i for i in items_list if i.access == "public"]
    if kind == BucketKind.ORG:
        return [i for i in items_list if i.access in ("org", "public")]
    me = _infer_caller_id(items_list)
    if me is None:
        return []
    if kind == BucketKind.MINE:
        return [i for i in items_list if i.owner_id == me]
    if kind == BucketKind.SHARED:
        return [
            i
            for i in items_list
            if i.access == "org" and i.owner_id != me
        ]
    return []


# Item types QGIS can actually consume as a layer on the canvas.
# Everything else (forms, dashboards, web apps, themes, templates,
# pick lists, boundaries, etc.) is portal-only content that has no
# QGIS rendering; hiding it from the Browser tree + search dock
# avoids cluttering both surfaces with rows the user can do
# nothing with from QGIS. The dock's "Open item properties"
# action would still work on filtered-out items, but the wider
# portal UI is the right place to read them; the plugin's job is
# to bring layers onto the canvas.
#
# Keep this list in sync with the dispatch in browser/items.py:
# _make_item() -- if a type renders as a draggable layer there,
# it belongs here too. Both spellings (kebab + snake) accepted
# defensively even though current portal API serializes snake.
QGIS_CONSUMABLE_TYPES: frozenset[str] = frozenset({
    "data_layer",
    "data-layer",
    "derived_layer",
    "derived-layer",
    "tile_layer",
    "tile-layer",
    "basemap",
    # ArcGIS / OGC connected services are XYZ / WMS / WFS sources
    # QGIS speaks natively, even though we don't yet have a
    # dedicated dispatch branch for each in _make_item.
    "arcgis_service",
    "arcgis-service",
    "wms_service",
    "wms-service",
    "wfs_service",
    "wfs-service",
    "service",
})


def is_qgis_consumable(item: ItemSummary) -> bool:
    """True iff the item's type is something QGIS can pull into the
    canvas as a layer. Used by the Browser tree + search dock to
    hide portal-only content (forms, dashboards, web apps, themes,
    etc.) that the plugin has no way to render.
    """
    return (item.type or "") in QGIS_CONSUMABLE_TYPES


def _infer_caller_id(items: list[ItemSummary]) -> str | None:
    """Most common owner_id among the private items in the result.

    The portal-side share filter already removed items the caller
    can't read, so a `private` item in the result must be owned by
    the caller. We pick the most frequent owner_id across the
    private subset rather than the first one as a small hedge
    against future API tweaks that might surface readable-via-
    explicit-share private items.
    """
    private = [i for i in items if i.access == "private"]
    if not private:
        return None
    counts: dict[str, int] = {}
    for it in private:
        counts[it.owner_id] = counts.get(it.owner_id, 0) + 1
    # max() with a tie-breaker (newer items_first) avoids
    # nondeterministic results across runs.
    return max(counts.keys(), key=lambda oid: counts[oid])
