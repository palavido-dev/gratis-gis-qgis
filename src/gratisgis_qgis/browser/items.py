# SPDX-License-Identifier: AGPL-3.0-or-later
"""QGIS Browser-tree items for the GratisGIS plugin.

The tree shape:

    GratisGIS                        (RootItem)
    +-- <connection-name>            (ConnectionItem)
        +-- My Content               (BucketItem, kind="mine")
        +-- Shared with Me           (BucketItem, kind="shared")
        +-- Org Content              (BucketItem, kind="org")
        +-- Public                   (BucketItem, kind="public")
            +-- <data_layer item>    (DataLayerItem)
            +-- <map item>           (MapItem)
            +-- ...

QGIS Browser items expand lazily via `createChildren()`. We cache
the fetched item list on the bucket so re-expanding is cheap; a
"Refresh" right-click on the bucket clears the cache and re-pulls.

Drag-to-canvas works automatically because each leaf is a
`QgsLayerItem` with a layer type + URI. For v3 data_layers we
hand QGIS an OGC API Features URI pointing at
`/api/public/ogc/collections/<itemId>` (the public surface; the
authed user can still read it because they own the item, but the
OGC route doesn't gate on auth -- which is fine because gating
happens at the items level on the portal side).
"""
from __future__ import annotations

from qgis.core import (  # type: ignore[import-not-found]
    QgsDataCollectionItem,
    QgsDataItem,
    QgsLayerItem,
    QgsMimeDataUtils,
)
from qgis.PyQt.QtCore import QCoreApplication  # type: ignore[import-not-found]

from gratisgis_client.models.item import ItemSummary

from ..log import get_logger
from ..settings import ConnectionProfile, ConnectionStore
from .buckets import BucketKind, all_buckets, filter_for_bucket
from .fetch import list_items_sync
from .uris import oapif_uri, vector_tile_uri

_log = get_logger(__name__)


# Display labels for the bucket discriminators. The bucket enum
# itself lives in `buckets.py` so the filtering logic can be
# tested without QGIS in the import path; the user-facing labels
# stay here next to the rest of the tree rendering.
_BUCKET_LABELS = {
    BucketKind.MINE: "My Content",
    BucketKind.SHARED: "Shared with Me",
    BucketKind.ORG: "Org Content",
    BucketKind.PUBLIC: "Public",
}


class RootItem(QgsDataCollectionItem):
    """The 'GratisGIS' root row in the Browser panel."""

    def __init__(self, parent: QgsDataItem | None, store: ConnectionStore) -> None:
        super().__init__(parent, "GratisGIS", "gratisgis:/")
        self._store = store
        self.setCapabilitiesV2(QgsDataItem.Fertile | QgsDataItem.Fast)

    def createChildren(self) -> list[QgsDataItem]:
        children: list[QgsDataItem] = []
        for name in self._store.list_names():
            profile = self._store.get(name)
            if profile is None:
                continue
            children.append(ConnectionItem(self, profile))
        if not children:
            children.append(
                _MessageItem(
                    self,
                    "No connections yet. Use 'Manage GratisGIS connections...' "
                    "to add one.",
                )
            )
        return children


class ConnectionItem(QgsDataCollectionItem):
    """One configured portal. Expands into the four scope buckets."""

    def __init__(self, parent: QgsDataItem, profile: ConnectionProfile) -> None:
        super().__init__(
            parent,
            profile.display_label,
            f"gratisgis:/{profile.name}",
        )
        self._profile = profile
        self.setCapabilitiesV2(QgsDataItem.Fertile | QgsDataItem.Fast)

    @property
    def profile(self) -> ConnectionProfile:
        return self._profile

    def createChildren(self) -> list[QgsDataItem]:
        if not self._profile.is_discovered:
            return [
                _MessageItem(
                    self,
                    "Connection not signed in yet. Open the connection "
                    "manager to sign in.",
                )
            ]
        if not self._profile.authcfg_id:
            return [
                _MessageItem(
                    self,
                    "No saved sign-in for this connection. Sign in via the "
                    "connection manager.",
                )
            ]
        return [
            BucketItem(self, self._profile, kind=kind)
            for kind in all_buckets()
        ]


class BucketItem(QgsDataCollectionItem):
    """A per-scope folder under a connection. Lazily fetches the
    matching items the first time it expands.
    """

    def __init__(
        self, parent: QgsDataItem, profile: ConnectionProfile, *, kind: str
    ) -> None:
        label = _BUCKET_LABELS.get(kind, kind)
        super().__init__(
            parent,
            label,
            f"gratisgis:/{profile.name}/{kind}",
        )
        self._profile = profile
        self._kind = kind
        self.setCapabilitiesV2(QgsDataItem.Fertile)

    def createChildren(self) -> list[QgsDataItem]:
        # Bucket discriminator -> list-call shape. The portal's
        # /api/items list endpoint already applies sharing rules
        # server-side, so we only have to add an access filter for
        # the public bucket (which would otherwise dilute with org
        # + private items the user owns) and an owner filter for
        # the My Content bucket. "Shared with Me" is approximated
        # as "everything access=org not owned by me" until the
        # share roster is exposed cleanly through the API; the
        # plugin filters client-side.
        try:
            items = list_items_sync(self._profile)
        except Exception as e:  # pragma: no cover - defensive
            _log.exception("BucketItem.createChildren list failed")
            return [_MessageItem(self, f"Failed to load: {e}")]

        items = list(filter_for_bucket(items, self._kind))
        if not items:
            return [_MessageItem(self, _empty_bucket_label(self._kind))]

        children: list[QgsDataItem] = []
        for it in sorted(items, key=lambda i: i.title.lower()):
            child = _make_item(self, self._profile, it)
            if child is not None:
                children.append(child)
        return children


class DataLayerItem(QgsLayerItem):
    """A v3 data_layer item. Drag adds an OGC API Features layer
    pointing at the portal's public collection endpoint.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
    ) -> None:
        # OAPIF URI shape lives in browser/uris.py so the same
        # builder serves the Search dock's add-to-canvas action.
        uri = oapif_uri(profile.portal_url, item.id)
        super().__init__(
            parent,
            item.title,
            f"gratisgis-data-layer:/{profile.name}/{item.id}",
            uri,
            QgsLayerItem.Vector,
            "OAPIF",
        )
        self._item = item

    @property
    def item(self) -> ItemSummary:
        return self._item

    def mimeUris(self) -> list[QgsMimeDataUtils.Uri]:
        u = QgsMimeDataUtils.Uri()
        u.layerType = "vector"
        u.providerKey = "OAPIF"
        u.uri = self.uri()
        u.name = self._item.title
        u.supportedCrs = ["EPSG:4326"]
        u.supportedFormats = ["application/geo+json"]
        return [u]


class TileLayerItem(QgsLayerItem):
    """A v3 data_layer rendered as MVT through the portal's public
    Tiles endpoint. Used for layers explicitly published as tiles
    OR as a future opt-in when a layer is too big for GeoJSON.
    Phase 1 emits it only on `tile_layer` typed items.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
    ) -> None:
        # Vector-tile URI lives in browser/uris.py; QGIS's
        # vector-tile provider reads the {z}/{y}/{x} template.
        uri = vector_tile_uri(profile.portal_url, item.id)
        super().__init__(
            parent,
            item.title,
            f"gratisgis-tile-layer:/{profile.name}/{item.id}",
            uri,
            QgsLayerItem.VectorTile,
            "vectortile",
        )
        self._item = item

    @property
    def item(self) -> ItemSummary:
        return self._item


class GenericItem(QgsDataItem):
    """Catch-all leaf for item types we don't yet expose as
    QGIS-consumable layers (form, dashboard, web_app, ...). Renders
    a row in the tree so the user can see it exists but it isn't
    draggable to the canvas.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
    ) -> None:
        super().__init__(
            QgsDataItem.NoCapabilities,
            parent,
            f"{item.title}  ({item.type})",
            f"gratisgis-item:/{profile.name}/{item.id}",
        )
        self._item = item
        self.setState(QgsDataItem.Populated)


class _MessageItem(QgsDataItem):
    """Static informational row in the tree. Not draggable, not
    expandable. Used for empty states + load errors.
    """

    def __init__(self, parent: QgsDataItem, message: str) -> None:
        super().__init__(QgsDataItem.NoCapabilities, parent, message, "")
        self.setState(QgsDataItem.Populated)


# -----------------------------------------------------------
# Leaf-item routing helpers (filtering lives in buckets.py)
# -----------------------------------------------------------


def _empty_bucket_label(kind: str) -> str:
    if kind == BucketKind.MINE:
        return "No items yet. Publish a layer or save one in the portal."
    if kind == BucketKind.SHARED:
        return "Nothing shared with you yet."
    if kind == BucketKind.ORG:
        return "No org-shared items."
    if kind == BucketKind.PUBLIC:
        return "No public items."
    return "Empty."


def _make_item(
    parent: QgsDataItem,
    profile: ConnectionProfile,
    item: ItemSummary,
) -> QgsDataItem | None:
    """Pick the right leaf class for an item's type."""
    if item.type == "data_layer":
        return DataLayerItem(parent, profile, item)
    if item.type == "tile_layer":
        return TileLayerItem(parent, profile, item)
    if item.type in ("map", "web_app", "dashboard", "form", "report_template"):
        return GenericItem(parent, profile, item)
    # Skip non-portal items entirely.
    return None


def _tr(text: str) -> str:
    """Wrap user-visible strings for QGIS's i18n machinery, even
    though we don't yet ship a translation catalog. Cheap insurance
    against having to retrofit later. QGIS picks up wrapped strings
    automatically when pylupdate is run against the source tree.
    """
    return QCoreApplication.translate("gratisgis_qgis.browser", text)
