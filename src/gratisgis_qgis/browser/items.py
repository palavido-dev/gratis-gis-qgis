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

from urllib.parse import quote

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
from .buckets import (
    BucketKind,
    all_buckets,
    filter_for_bucket,
    is_qgis_consumable,
)
from .fetch import get_item_sync, list_items_sync
from .uris import oapif_uri, vector_tile_uri

_log = get_logger(__name__)


# -----------------------------------------------------------
# QGIS 3 / QGIS 4 compat for Browser-tree enum constants.
#
# QGIS 3 exposed Fertile / Fast / Populated as class-level
# attributes on QgsDataItem. QGIS 4 moved them under scoped
# Qgis.BrowserItemCapability / Qgis.BrowserItemType /
# Qgis.BrowserItemState enums and dropped the QgsDataItem
# shortcuts under strict PyQt6.
#
# The BrowserItemType enum membership also changed: QGIS 4 has
# Collection / Directory / Layer / Error / Favorites / Project /
# Custom / Fields / Field (no NoType). For our leaf-item types
# (GenericItem, _MessageItem) Custom is the right fit -- we're
# not a Layer (not draggable to canvas) but we are a tree node.
#
# Each lookup goes through a small helper that tries the scoped
# Qgis path first, then the old QgsDataItem attr, then a list of
# fallback names. Per-call sites stay readable; future QGIS
# revisions that shuffle enum members again get a clear
# AttributeError pointing at the resolver, not random call sites.
# -----------------------------------------------------------


def _resolve_enum(*candidates: tuple[object, str]) -> object:
    """Try each (holder, attribute_name) pair until one resolves.

    Raises AttributeError listing every attempted path if none
    match, so a future Qt / QGIS shuffle gives a clean error
    pointing at the resolver instead of a per-call-site mystery.
    """
    tried: list[str] = []
    for holder, attr in candidates:
        if holder is None:
            continue
        tried.append(f"{getattr(holder, '__name__', holder)}.{attr}")
        if hasattr(holder, attr):
            return getattr(holder, attr)
    raise AttributeError(
        f"None of these resolve to a usable enum value: {', '.join(tried)}"
    )


try:
    from qgis.core import Qgis  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover -- tests don't hit Qgis directly
    Qgis = None  # type: ignore[assignment]

# Use Custom as our leaf-item BrowserItemType: it's the
# documented "non-Directory, non-Layer, plugin-defined node"
# value, present on both QGIS 3 (via QgsDataItem.Custom) and
# QGIS 4 (via Qgis.BrowserItemType.Custom).
_BROWSER_TYPE_NO_TYPE = _resolve_enum(
    (getattr(Qgis, "BrowserItemType", None) if Qgis else None, "Custom"),
    (getattr(QgsDataItem, "Type", None), "Custom"),
    (QgsDataItem, "Custom"),
    (QgsDataItem, "NoType"),
)
_BROWSER_CAP_FERTILE = _resolve_enum(
    (getattr(Qgis, "BrowserItemCapability", None) if Qgis else None, "Fertile"),
    (QgsDataItem, "Fertile"),
)
_BROWSER_CAP_FAST = _resolve_enum(
    (getattr(Qgis, "BrowserItemCapability", None) if Qgis else None, "Fast"),
    (QgsDataItem, "Fast"),
)
_POPULATED_STATE = _resolve_enum(
    (getattr(Qgis, "BrowserItemState", None) if Qgis else None, "Populated"),
    (QgsDataItem, "Populated"),
)


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
        self.setCapabilitiesV2(_BROWSER_CAP_FERTILE | _BROWSER_CAP_FAST)

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
        self.setCapabilitiesV2(_BROWSER_CAP_FERTILE | _BROWSER_CAP_FAST)

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
        self.setCapabilitiesV2(_BROWSER_CAP_FERTILE)

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
        # Trim down to types QGIS can actually render. Portal-only
        # surfaces (forms, dashboards, web apps, themes, templates,
        # pick lists, boundaries) get hidden here -- they're real
        # portal items but there's no canvas action for them, so
        # surfacing them in the Browser tree just adds noise.
        items = [i for i in items if is_qgis_consumable(i)]
        if not items:
            return [_MessageItem(self, _empty_bucket_label(self._kind))]

        # Group by normalized type so the user sees "Data layers
        # (12)" / "Tile layers (3)" / "Basemaps (7)" rather than
        # one 100-row flat list mixing types. Each group expands
        # to its sorted-by-title members.
        by_type: dict[str, list[ItemSummary]] = {}
        for it in items:
            key = _normalize_type(it.type)
            by_type.setdefault(key, []).append(it)

        children: list[QgsDataItem] = []
        for type_key in sorted(by_type.keys(), key=_type_sort_key):
            group_items = sorted(by_type[type_key], key=lambda i: i.title.lower())
            children.append(
                _TypeGroupItem(
                    self,
                    self._profile,
                    type_key=type_key,
                    items=group_items,
                )
            )
        return children


def _normalize_type(t: str | None) -> str:
    """Collapse kebab + snake spellings to a single snake key so
    grouping doesn't split data-layer and data_layer into two
    siblings.
    """
    return (t or "unknown").replace("-", "_")


# Group display labels (plural form for the type-group nodes).
# Falls back to a title-cased version of the type for any new type
# the portal grows that we haven't labelled here yet.
_TYPE_GROUP_LABELS: dict[str, str] = {
    "data_layer": "Data layers",
    "derived_layer": "Derived layers",
    "tile_layer": "Tile layers",
    "basemap": "Basemaps",
    "arcgis_service": "ArcGIS services",
    "wms_service": "WMS services",
    "wfs_service": "WFS services",
    "service": "Connected services",
}


# Sort order for type groups: most-used / most-canvas-relevant on
# top. Anything not in this map sorts after the named entries,
# alphabetically.
_TYPE_GROUP_ORDER: dict[str, int] = {
    "data_layer": 10,
    "derived_layer": 20,
    "tile_layer": 30,
    "basemap": 40,
    "arcgis_service": 50,
    "service": 60,
    "wms_service": 70,
    "wfs_service": 80,
}


def _type_sort_key(type_key: str) -> tuple[int, str]:
    """Sort groups by the explicit order first, label alpha second."""
    return (_TYPE_GROUP_ORDER.get(type_key, 1000), type_key)


def _type_group_label(type_key: str, count: int) -> str:
    """Render the user-facing label like 'Data layers (12)'."""
    base = _TYPE_GROUP_LABELS.get(type_key, type_key.replace("_", " ").title())
    return f"{base}  ({count})"


class _TypeGroupItem(QgsDataCollectionItem):
    """A per-type sub-node under a BucketItem. Holds the already-
    fetched, already-bucket-filtered items so expansion is instant
    (no re-fetch).
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        *,
        type_key: str,
        items: list[ItemSummary],
    ) -> None:
        label = _type_group_label(type_key, len(items))
        super().__init__(
            parent,
            label,
            f"{parent.path()}/{type_key}",
        )
        self._profile = profile
        self._items = items
        # Fertile so the user can expand; Fast because expansion
        # is just dispatching the in-memory list (no I/O).
        self.setCapabilitiesV2(_BROWSER_CAP_FERTILE | _BROWSER_CAP_FAST)

    def createChildren(self) -> list[QgsDataItem]:
        children: list[QgsDataItem] = []
        for it in self._items:
            child = _make_item(self, self._profile, it)
            if child is not None:
                children.append(child)
        return children


class DataLayerItem(QgsDataCollectionItem):
    """A v3 data_layer item, rendered as a collection node that
    expands into one child per sublayer.

    Why a collection and not a leaf:

      A v3 ``data_layer`` can carry multiple layers (a polygon
      layer + a related table sublayer, for example: the WV
      Parcels item has ``MasterSurfWV_2025`` + ``ParcelSummary``).
      The portal's OGC controller already publishes one collection
      per layer with id ``<itemId>__<layerKey>``, plus a bare-UUID
      alias for the first layer for v1 back-compat. Surfacing the
      sublayers in the Browser tree means the user sees what's
      actually there instead of silently getting the first layer
      whenever they drag the item.

      Single-layer items still render as a collection with one
      child rather than a special-case leaf: the small extra-click
      cost is worth the consistent UX. The child label uses the
      layer's label (or layer id) so single-layer items still read
      naturally.

    Sublayer discovery is lazy. ``ItemSummary`` (what the items
    list endpoint returns) doesn't include the ``data`` envelope,
    so we fetch the full item on first expand. Subsequent
    expansions reuse QGIS's cached child list.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
    ) -> None:
        super().__init__(
            parent,
            item.title,
            f"gratisgis-data-layer:/{profile.name}/{item.id}",
        )
        self._profile = profile
        self._item = item
        self.setCapabilitiesV2(_BROWSER_CAP_FERTILE)

    @property
    def item(self) -> ItemSummary:
        return self._item

    def createChildren(self) -> list[QgsDataItem]:
        full = get_item_sync(self._profile, self._item.id) or {}
        layers = _extract_v3_layers(full)
        if not layers:
            # No layers found in the data envelope. Fall back to
            # the bare-UUID alias the portal exposes for v1 back-
            # compat. One leaf, default label is the item title.
            return [
                _DataLayerSublayerItem(
                    self,
                    self._profile,
                    self._item,
                    collection_id=self._item.id,
                    label=self._item.title,
                    has_geometry=True,
                )
            ]
        children: list[QgsDataItem] = []
        for lyr in layers:
            layer_id = str(lyr.get("id") or "")
            if not layer_id:
                continue
            label = str(lyr.get("label") or layer_id)
            collection_id = f"{self._item.id}__{layer_id}"
            # Spatial sublayers (geometryType present) default to
            # vector tiles for fast viewing of huge datasets like
            # WV Parcels at WV-extent zoom. Non-spatial tables
            # (geometryType is null/absent) can't render as MVT;
            # they fall through to OAPIF so QGIS can still pull
            # rows into the attribute table.
            has_geometry = isinstance(lyr.get("geometryType"), str) and (
                str(lyr.get("geometryType")) != ""
            )
            children.append(
                _DataLayerSublayerItem(
                    self,
                    self._profile,
                    self._item,
                    collection_id=collection_id,
                    label=label,
                    has_geometry=has_geometry,
                )
            )
        return children


def _extract_v3_layers(full_item: dict[str, object]) -> list[dict[str, object]]:
    """Pull the v3 layers array out of a full item's data envelope.

    Mirrors `pickV3Layers` in
    `apps/portal-api/src/public/ogc/features.controller.ts`: v3
    items have ``data.version == 3`` and ``data.layers`` is a list
    of ``{id, label?}``. Anything else returns empty so the caller
    falls back to the bare-UUID alias.
    """
    data = full_item.get("data") if isinstance(full_item, dict) else None
    if not isinstance(data, dict):
        return []
    if data.get("version") != 3:
        return []
    layers = data.get("layers")
    if not isinstance(layers, list):
        return []
    out: list[dict[str, object]] = []
    for lyr in layers:
        if isinstance(lyr, dict) and isinstance(lyr.get("id"), str):
            out.append(lyr)
    return out


class _DataLayerSublayerItem(QgsLayerItem):
    """One sublayer leaf under a DataLayerItem.

    Spatial sublayers (``has_geometry=True``) add as MVT vector
    tiles by default, pointing at the OGC API Tiles endpoint
    ``/collections/<collectionId>/tiles/WebMercatorQuad``. Vector
    tiles scale to county/state-extent zoom on huge layers like
    WV Parcels (1.4M polygons) where an OAPIF GeoJSON request
    would either time out or return a multi-megabyte unfiltered
    dump that QGIS can't render. The engine simplifies geometry
    and caps features per tile so low-zoom tiles complete in
    sub-second time; high zoom shows full detail.

    Non-spatial sublayers (tables, ``has_geometry=False``) can't
    render as MVT -- ST_AsMVTGeom skips them. They fall through
    to OAPIF so QGIS can still pull rows into an attribute table.

    Editing isn't supported on MVT layers (they're a read-only
    rendering format). The Editor menu's "Add as editable
    features" action is the OAPIF escape hatch for users who
    actually need to edit features on the canvas.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
        *,
        collection_id: str,
        label: str,
        has_geometry: bool,
    ) -> None:
        if has_geometry:
            uri = vector_tile_uri(profile.portal_url, collection_id)
            layer_type = QgsLayerItem.VectorTile
            provider_key = "vectortile"
        else:
            uri = oapif_uri(profile.portal_url, collection_id)
            layer_type = QgsLayerItem.Vector
            provider_key = "OAPIF"
        super().__init__(
            parent,
            label,
            f"gratisgis-data-layer-sublayer:/{profile.name}/{collection_id}",
            uri,
            layer_type,
            provider_key,
        )
        self._profile = profile
        self._item = item
        self._collection_id = collection_id
        self._label = label
        self._has_geometry = has_geometry
        self._provider_key = provider_key

    @property
    def item(self) -> ItemSummary:
        return self._item

    @property
    def collection_id(self) -> str:
        return self._collection_id

    def mimeUris(self) -> list[QgsMimeDataUtils.Uri]:
        u = QgsMimeDataUtils.Uri()
        if self._has_geometry:
            u.layerType = "vector-tile"
        else:
            u.layerType = "vector"
        u.providerKey = self._provider_key
        u.uri = self.uri()
        u.name = self._label
        if not self._has_geometry:
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


class BasemapItem(QgsLayerItem):
    """A portal `basemap` item. Drag adds an XYZ raster layer using
    the tile URL stored on item.data.

    Basemap data envelope shape (from packages/shared-types):
      { "kind": "tile-url", "tileUrl": "https://.../{z}/{y}/{x}",
        "attribution": "..." }

    The item-list endpoint returns only ItemSummary (no data), so
    we fetch the full envelope lazily on construction. With a
    typical ~10 basemaps per bucket, those fetches happen once on
    expand and the data is cached for the life of the tree node.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
    ) -> None:
        full = get_item_sync(profile, item.id) or {}
        data = full.get("data") if isinstance(full, dict) else None
        tile_url = ""
        if isinstance(data, dict):
            tile_url = str(data.get("tileUrl") or "")
        # QGIS's wms provider in XYZ mode needs the tile URL
        # URL-encoded inside the data-source URI. Without encoding,
        # the literal `{z}/{y}/{x}` braces and the `://` confuse the
        # URI key=value parser and QGIS rejects the source with
        # "not a valid or recognized data source".
        uri = (
            f"type=xyz&url={quote(tile_url, safe='')}&zmin=0&zmax=22"
            if tile_url
            else ""
        )
        super().__init__(
            parent,
            item.title,
            f"gratisgis-basemap:/{profile.name}/{item.id}",
            uri,
            QgsLayerItem.Raster,
            "wms",
        )
        self._item = item

    @property
    def item(self) -> ItemSummary:
        return self._item

    def mimeUris(self) -> list[QgsMimeDataUtils.Uri]:
        u = QgsMimeDataUtils.Uri()
        u.layerType = "raster"
        u.providerKey = "wms"
        u.name = self._item.title
        # Must be self.uri() (the real XYZ data-source URI passed to
        # the QgsLayerItem ctor), NOT self.path() (the Browser-tree
        # node identifier). Sending path() gave the user "gratisgis-
        # basemap:/... is not a valid or recognized data source".
        u.uri = self.uri()
        return [u]


class ServiceItem(QgsDataCollectionItem):
    """A portal ``service`` (Connected Service) item, rendered as a
    collection that expands into one child per sublayer.

    Connected services wrap an ArcGIS REST MapServer or
    FeatureServer endpoint. A MapServer commonly hosts 5-50
    individually addressable sublayers (counties, hydrography,
    roads, ...) and the user wants to add specific layers rather
    than drop the whole service onto the canvas as one opaque
    image.

    Service ``data`` envelope shape:

        {
          "url": "https://server/.../MapServer",
          "layers": [{ "id": "0", "name": "Counties", ... }, ...]
        }

    Each child here uses arcgisfeatureserver if the service is a
    FeatureServer (one layer per featureserver layer id) or
    arcgismapserver pointed at the per-layer URL for a MapServer.
    Layers with no id fall back to the bare service URL.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
    ) -> None:
        super().__init__(
            parent,
            item.title,
            f"gratisgis-service:/{profile.name}/{item.id}",
        )
        self._profile = profile
        self._item = item
        self.setCapabilitiesV2(_BROWSER_CAP_FERTILE)

    @property
    def item(self) -> ItemSummary:
        return self._item

    def createChildren(self) -> list[QgsDataItem]:
        full = get_item_sync(self._profile, self._item.id) or {}
        data = full.get("data") if isinstance(full, dict) else None
        base_url = ""
        layers: list[dict[str, object]] = []
        if isinstance(data, dict):
            base_url = str(data.get("url") or "")
            raw_layers = data.get("layers")
            if isinstance(raw_layers, list):
                layers = [lyr for lyr in raw_layers if isinstance(lyr, dict)]
        if not base_url:
            return [
                _MessageItem(
                    self,
                    "No service URL configured. Open the item in the "
                    "portal to set the endpoint.",
                )
            ]
        is_feature_server = "/FeatureServer" in base_url
        if not layers:
            # No sublayer metadata cached on the item -- fall back
            # to one leaf for the whole service. This is the safe
            # behaviour for services we haven't probed yet.
            return [
                _ServiceSublayerItem(
                    self,
                    self._profile,
                    self._item,
                    label=self._item.title,
                    layer_url=base_url,
                    is_feature_server=is_feature_server,
                )
            ]
        children: list[QgsDataItem] = []
        for lyr in layers:
            # The portal's connected-service data envelope mirrors
            # the ArcGIS REST shape: each layer carries `name`
            # (the layer's id used in REST URLs -- a string number
            # like "0", "1") and `title` (display name). There is
            # no separate `id` field on layers in this envelope, so
            # use `name` as the URL segment and `title` for the
            # tree label, falling back to `name` if no title.
            layer_id_raw = lyr.get("name")
            if layer_id_raw is None or layer_id_raw == "":
                continue
            layer_id = str(layer_id_raw)
            label = str(lyr.get("title") or lyr.get("label") or layer_id)
            # ArcGIS REST layer URLs are <baseUrl>/<layerId>, both
            # for MapServer (raster sublayer) and FeatureServer
            # (vector sublayer). The provider distinguishes via the
            # `/FeatureServer` vs `/MapServer` substring in the URL.
            layer_url = f"{base_url.rstrip('/')}/{layer_id}"
            children.append(
                _ServiceSublayerItem(
                    self,
                    self._profile,
                    self._item,
                    label=label,
                    layer_url=layer_url,
                    is_feature_server=is_feature_server,
                )
            )
        if not children:
            return [_MessageItem(self, "No sublayers found on this service.")]
        return children


class _ServiceSublayerItem(QgsLayerItem):
    """One ArcGIS REST sublayer leaf under a ServiceItem.

    Each leaf points at ``<baseUrl>/<layerId>`` so dragging it onto
    the canvas adds just that sublayer, not the whole service.
    arcgisfeatureserver -> vector, arcgismapserver -> raster.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
        *,
        label: str,
        layer_url: str,
        is_feature_server: bool,
    ) -> None:
        provider = "arcgisfeatureserver" if is_feature_server else "arcgismapserver"
        # arcgismapserver / arcgisfeatureserver providers want the
        # OAPIF-style ``key='value' key='value'`` shape, not the
        # XYZ ``key=value&key=value`` shape. Without the single
        # quotes QGIS pastes the value of the next key onto the
        # URL ("url=https://.../MapServer/0&crs=EPSG:3857" comes
        # back from the server as Invalid URL 400). The quotes
        # are how QGIS knows where one value ends and the next
        # key begins, matching the OAPIF builder in uris.py.
        uri = f"url='{layer_url}' crs='EPSG:3857'"
        super().__init__(
            parent,
            label,
            f"gratisgis-service-sublayer:/{profile.name}/{item.id}/{layer_url}",
            uri,
            QgsLayerItem.Vector if is_feature_server else QgsLayerItem.Raster,
            provider,
        )
        self._profile = profile
        self._item = item
        self._label = label
        self._layer_url = layer_url
        self._provider = provider

    @property
    def item(self) -> ItemSummary:
        return self._item

    def mimeUris(self) -> list[QgsMimeDataUtils.Uri]:
        u = QgsMimeDataUtils.Uri()
        u.layerType = (
            "vector" if self._provider == "arcgisfeatureserver" else "raster"
        )
        u.providerKey = self._provider
        u.name = self._label
        u.uri = self.uri()
        return [u]


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
        # QGIS 4 tightened argument typing: the first arg is now
        # the *type* (BrowserItemType, e.g. NoType / Collection /
        # Layer), not the capabilities flag set. QGIS 3 accepted
        # an int there and our old code passed NoCapabilities,
        # which silently worked but became a TypeError on QGIS 4.
        super().__init__(
            _BROWSER_TYPE_NO_TYPE,
            parent,
            f"{item.title}  ({item.type})",
            f"gratisgis-item:/{profile.name}/{item.id}",
        )
        self._item = item
        self.setState(_POPULATED_STATE)


class _MessageItem(QgsDataItem):
    """Static informational row in the tree. Not draggable, not
    expandable. Used for empty states + load errors.
    """

    def __init__(self, parent: QgsDataItem, message: str) -> None:
        super().__init__(_BROWSER_TYPE_NO_TYPE, parent, message, "")
        self.setState(_POPULATED_STATE)


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
    """Pick the right leaf class for an item's type.

    Accepts both the kebab-case spellings the portal currently
    emits (``data-layer``, ``tile-layer``, ``web-app``) and the
    historical snake_case spellings the older schema used
    (``data_layer``, ``tile_layer``, ``web_app``). Anything we
    don't have a dedicated class for renders through the generic
    leaf so the user can at least see the item exists.
    """
    # Normalize hyphens to underscores so a single comparison
    # handles both shapes.
    t = (item.type or "").replace("-", "_")
    if t == "data_layer":
        return DataLayerItem(parent, profile, item)
    if t == "tile_layer":
        return TileLayerItem(parent, profile, item)
    if t == "basemap":
        return BasemapItem(parent, profile, item)
    # Connected services: today every "service" item on prod is an
    # ArcGIS REST MapServer; the dedicated *_service legacy types
    # also flow through the same handler since they all carry a
    # `url` data field. WFS / WMS specialisation can split out once
    # the dispatch grows; for now ServiceItem auto-detects
    # MapServer vs FeatureServer via the URL suffix.
    if t in ("service", "arcgis_service", "wms_service", "wfs_service"):
        return ServiceItem(parent, profile, item)
    # Generic display for every other type the portal returns. The
    # Browser tree shows them with a default icon; double-click goes
    # to the item-properties dialog instead of an add-to-canvas
    # action that wouldn't apply.
    return GenericItem(parent, profile, item)


def _tr(text: str) -> str:
    """Wrap user-visible strings for QGIS's i18n machinery, even
    though we don't yet ship a translation catalog. Cheap insurance
    against having to retrofit later. QGIS picks up wrapped strings
    automatically when pylupdate is run against the source tree.
    """
    return QCoreApplication.translate("gratisgis_qgis.browser", text)
