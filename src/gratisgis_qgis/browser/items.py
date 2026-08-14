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
`QgsLayerItem` with a layer type + URI. Spatial data_layer
sublayers render as vector tiles: public items point at the public
OGC Tiles surface (so a saved project keeps working for anonymous
viewers), while private and org items point at the portal's authed
per-layer MVT route with the connection's layer authcfg attached,
which is what makes non-public layers actually draw. Non-spatial
sublayers fall through to OAPIF, a public-only surface; private
tables therefore list with a tooltip pointing at the Clone flow.
"""
from __future__ import annotations

from urllib.parse import quote

from qgis.core import (  # type: ignore[import-not-found]
    QgsDataCollectionItem,
    QgsDataItem,
    QgsLayerItem,
    QgsMimeDataUtils,
)

from gratisgis_client.models.item import ItemSummary

from ..log import get_logger
from ..portal import get_item, list_items
from ..qgis_compat import resolve_enum
from ..raster_auth import configure_gdal_auth
from ..settings import ConnectionProfile, ConnectionStore
from .buckets import (
    BucketKind,
    all_buckets,
    filter_for_bucket,
    is_qgis_consumable,
)
from .uris import (
    authed_vector_tile_uri,
    oapif_uri,
    tile_layer_cog_uri,
    tile_layer_xyz_uri,
    vector_tile_uri,
)

_log = get_logger(__name__)


# -----------------------------------------------------------
# QGIS 3 / QGIS 4 compat for Browser-tree enum constants.
#
# QGIS 3 exposed Fertile / Fast / Populated / Vector / Raster /
# VectorTile as class-level attributes on QgsDataItem and
# QgsLayerItem. QGIS 4 moved them under scoped
# Qgis.BrowserItemCapability / Qgis.BrowserItemType /
# Qgis.BrowserItemState / Qgis.BrowserLayerType enums and dropped
# the class-level shortcuts under strict PyQt6.
#
# The BrowserItemType enum membership also changed: QGIS 4 has
# Collection / Directory / Layer / Error / Favorites / Project /
# Custom / Fields / Field (no NoType). For our leaf-item types
# (GenericItem, _MessageItem) Custom is the right fit -- we're
# not a Layer (not draggable to canvas) but we are a tree node.
#
# Each lookup goes through the shared resolver (qgis_compat),
# scoped Qgis path first, then the old class attr, then fallback
# names. Per-call sites stay readable; future QGIS revisions that
# shuffle enum members again get a clear AttributeError pointing
# at the resolver, not random call sites.
# -----------------------------------------------------------


try:
    from qgis.core import Qgis  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover -- tests don't hit Qgis directly
    Qgis = None  # type: ignore[assignment]

# Use Custom as our leaf-item BrowserItemType: it's the
# documented "non-Directory, non-Layer, plugin-defined node"
# value, present on both QGIS 3 (via QgsDataItem.Custom) and
# QGIS 4 (via Qgis.BrowserItemType.Custom).
_BROWSER_TYPE_NO_TYPE = resolve_enum(
    (getattr(Qgis, "BrowserItemType", None) if Qgis else None, "Custom"),
    (getattr(QgsDataItem, "Type", None), "Custom"),
    (QgsDataItem, "Custom"),
    (QgsDataItem, "NoType"),
)
_BROWSER_CAP_FERTILE = resolve_enum(
    (getattr(Qgis, "BrowserItemCapability", None) if Qgis else None, "Fertile"),
    (QgsDataItem, "Fertile"),
)
_BROWSER_CAP_FAST = resolve_enum(
    (getattr(Qgis, "BrowserItemCapability", None) if Qgis else None, "Fast"),
    (QgsDataItem, "Fast"),
)
_POPULATED_STATE = resolve_enum(
    (getattr(Qgis, "BrowserItemState", None) if Qgis else None, "Populated"),
    (QgsDataItem, "Populated"),
)

# Layer-type values for QgsLayerItem construction. QGIS 3.20 moved
# them to the scoped Qgis.BrowserLayerType enum and QGIS 4 under
# strict PyQt6 drops the old QgsLayerItem class-level shortcuts
# (Vector / Raster / VectorTile as class attributes), so every ctor
# call routes through the same resolver as the capability flags
# above instead of touching the unscoped spellings directly.
_LAYER_TYPE_VECTOR = resolve_enum(
    (getattr(Qgis, "BrowserLayerType", None) if Qgis else None, "Vector"),
    (QgsLayerItem, "Vector"),
)
_LAYER_TYPE_RASTER = resolve_enum(
    (getattr(Qgis, "BrowserLayerType", None) if Qgis else None, "Raster"),
    (QgsLayerItem, "Raster"),
)
_LAYER_TYPE_VECTOR_TILE = resolve_enum(
    (getattr(Qgis, "BrowserLayerType", None) if Qgis else None, "VectorTile"),
    (QgsLayerItem, "VectorTile"),
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
            children.append(ConnectionItem(self, self._store, name))
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
    """One configured portal. Expands into the four scope buckets.

    Holds the connection's NAME and the store, never a
    ``ConnectionProfile``. A profile is a frozen snapshot, and this
    item outlives the state it describes: signing out changes the
    profile but not this node's name or path, and QGIS's refresh keeps
    an existing child whose name and path still match rather than
    replacing it with the one ``createChildren`` just built. So a
    captured profile survives sign-out, the node keeps reporting a
    signed-in connection, and the tree offers private layers to a
    signed-out user. Reading the store on each expand is what makes the
    node describe the connection as it is now.
    """

    def __init__(
        self, parent: QgsDataItem, store: ConnectionStore, name: str
    ) -> None:
        profile = store.get(name)
        super().__init__(
            parent,
            profile.display_label if profile else name,
            f"gratisgis:/{name}",
        )
        self._store = store
        self._name = name
        self.setCapabilitiesV2(_BROWSER_CAP_FERTILE | _BROWSER_CAP_FAST)

    @property
    def profile(self) -> ConnectionProfile | None:
        """The connection as the store has it right now, or None.

        None means the connection was deleted while its node was still
        on screen, which the Browser tree allows: the connection
        manager is a separate dialog and nothing forces a refresh.
        """
        return self._store.get(self._name)

    def createChildren(self) -> list[QgsDataItem]:
        profile = self.profile
        if profile is None:
            return [
                _MessageItem(
                    self,
                    "This connection has been deleted. Refresh the Browser "
                    "panel to remove it.",
                )
            ]
        if not profile.is_discovered:
            return [
                _MessageItem(
                    self,
                    "Connection not signed in yet. Open the connection "
                    "manager to sign in.",
                )
            ]
        if not profile.authcfg_id:
            return [
                _MessageItem(
                    self,
                    "No saved sign-in for this connection. Sign in via the "
                    "connection manager.",
                )
            ]
        return [
            BucketItem(self, self._store, self._name, kind=kind)
            for kind in all_buckets()
        ]


class BucketItem(QgsDataCollectionItem):
    """A per-scope folder under a connection. Lazily fetches the
    matching items the first time it expands.

    Carries the connection name rather than a profile, for the reason
    given on ``ConnectionItem``: the bucket's path does not change when
    the user signs out, so QGIS keeps this node across a refresh and a
    captured profile would still be the signed-in one.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        store: ConnectionStore,
        name: str,
        *,
        kind: str,
    ) -> None:
        label = _BUCKET_LABELS.get(kind, kind)
        super().__init__(
            parent,
            label,
            f"gratisgis:/{name}/{kind}",
        )
        self._store = store
        self._name = name
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
        profile = self._store.get(self._name)
        # Signed out, or the connection is gone. Checked here rather
        # than left to the client so the row reads as a state the user
        # put the plugin in, not as a red "Failed to load" error: this
        # is what a signed-out tree is supposed to look like.
        if profile is None or not profile.authcfg_id:
            return [_MessageItem(self, "Not signed in.")]

        try:
            items = list_items(profile)
        except Exception as e:  # pragma: no cover - defensive
            _log.exception("BucketItem.createChildren list failed")
            return [_MessageItem(self, f"Failed to load: {e}")]

        items = list(
            filter_for_bucket(
                items,
                self._kind,
                # The sub claim captured at sign-in; empty for
                # profiles that predate it, in which case the
                # filter falls back to ownership inference.
                caller_id=profile.user_id or None,
            )
        )
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
                    profile,
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
        # Fertile so the user can expand. Fast means "createChildren
        # is instant, run it on the GUI thread"; that holds for every
        # group except basemaps, whose children each need the item's
        # data envelope fetched (see _make_item), so basemap groups
        # keep the Browser worker thread by not claiming Fast.
        caps = _BROWSER_CAP_FERTILE
        # Basemaps and tile layers each need the item's data
        # envelope fetched per child, so they must not claim Fast
        # (which would run createChildren on the GUI thread).
        if type_key not in ("basemap", "tile_layer"):
            caps = caps | _BROWSER_CAP_FAST
        self.setCapabilitiesV2(caps)

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
        full = get_item(self._profile, self._item.id) or {}
        layers = _extract_v3_layers(full)
        if not layers:
            # No layers found in the data envelope. Fall back to
            # the bare-UUID alias the portal exposes for v1 back-
            # compat. One leaf, default label is the item title.
            # No per-layer id exists on those old shapes, so the
            # authed tile route (which needs one) is unavailable
            # and the leaf stays on the public surface.
            return [
                _DataLayerSublayerItem(
                    self,
                    self._profile,
                    self._item,
                    collection_id=self._item.id,
                    label=self._item.title,
                    has_geometry=True,
                    layer_id=None,
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
                    layer_id=layer_id,
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


def _spatial_sublayer_uri(
    profile: ConnectionProfile,
    item: ItemSummary,
    *,
    collection_id: str,
    layer_id: str | None,
) -> str:
    """Pick the vector-tile endpoint for a spatial sublayer.

    Public items stay on the public OGC Tiles surface so a project
    file saved with the layer keeps rendering for anonymous viewers
    who never signed in. Anything else uses the portal's authed
    per-layer MVT route with the connection's layer authcfg attached,
    which is what makes private and org layers actually draw instead
    of listing in the tree and rendering empty. Both degradations
    (sign-in could not mint a layer key, or a v1/v2 item with no
    per-layer id for the authed route to address) fall back to the
    public surface, where a non-public layer behaves exactly as it
    did before authed rendering existed.
    """
    if item.access == "public":
        return vector_tile_uri(profile.portal_url, collection_id, extent=item.bbox)
    if profile.layer_authcfg_id and layer_id:
        return authed_vector_tile_uri(
            profile.portal_url,
            item.id,
            layer_id,
            authcfg_id=profile.layer_authcfg_id,
            extent=item.bbox,
        )
    _log.debug(
        "Non-public sublayer %s falls back to the public tiles surface "
        "(layer authcfg present: %s, layer id: %r)",
        collection_id,
        bool(profile.layer_authcfg_id),
        layer_id,
    )
    return vector_tile_uri(profile.portal_url, collection_id, extent=item.bbox)


class _DataLayerSublayerItem(QgsLayerItem):
    """One sublayer leaf under a DataLayerItem.

    Spatial sublayers (``has_geometry=True``) add as MVT vector
    tiles by default. Vector tiles scale to county/state-extent
    zoom on huge layers like WV Parcels (1.4M polygons) where an
    OAPIF GeoJSON request would either time out or return a
    multi-megabyte unfiltered dump that QGIS can't render. The
    engine simplifies geometry and caps features per tile so
    low-zoom tiles complete in sub-second time; high zoom shows
    full detail. Which tile endpoint depends on the item's access;
    see ``_spatial_sublayer_uri``.

    Non-spatial sublayers (tables, ``has_geometry=False``) can't
    render as MVT -- ST_AsMVTGeom skips them. They fall through
    to OAPIF so QGIS can still pull rows into an attribute table.
    OAPIF is the PUBLIC surface, and no authed table endpoint
    exists server-side (a documented portal follow-up), so private
    tables stay listed but get a tooltip pointing at the Clone
    flow, which reads them through the authed session.

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
        layer_id: str | None,
    ) -> None:
        if has_geometry:
            uri = _spatial_sublayer_uri(
                profile, item, collection_id=collection_id, layer_id=layer_id
            )
            layer_type = _LAYER_TYPE_VECTOR_TILE
            provider_key = "vectortile"
        else:
            uri = oapif_uri(profile.portal_url, collection_id)
            layer_type = _LAYER_TYPE_VECTOR
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
        if not has_geometry and item.access != "public":
            self.setToolTip(
                "Private table: rows are not readable through the public "
                "OGC surface, so this layer will load empty. Use 'Clone "
                "layer for offline use' to work with private tables."
            )

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
    """A ``tile_layer`` item: a RASTER published as a tile pyramid.

    This used to build the portal's public vector-tile URI, which was
    simply the wrong surface: a tile_layer is imagery or a derived
    elevation product (a COG or a PMTiles archive, always
    ``kind: raster``), and the vector-tile collection it pointed at
    does not exist for these items. Every tile_layer therefore added a
    layer that drew nothing, with no error to explain it.

    COG-backed items open through GDAL's ``/vsicurl`` (range requests
    are supported end to end, so only the needed overview tiles are
    fetched). PMTiles-backed items are NOT constructed as layers at
    all; see ``UnsupportedTileLayerItem`` for why.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        uri = tile_layer_cog_uri(profile.portal_url, item.id)
        super().__init__(
            parent,
            item.title,
            f"gratisgis-tile-layer:/{profile.name}/{item.id}",
            uri,
            _LAYER_TYPE_RASTER,
            "gdal",
        )
        self._item = item
        self._data = data or {}
        # The credential has to reach GDAL before QGIS opens the
        # dataset, and the drop happens outside our code, so register
        # it as the node is built rather than at drop time.
        configure_gdal_auth(profile)

    @property
    def item(self) -> ItemSummary:
        return self._item

    def mimeUris(self) -> list[QgsMimeDataUtils.Uri]:
        u = QgsMimeDataUtils.Uri()
        u.layerType = "raster"
        u.providerKey = "gdal"
        u.name = self._item.title
        # self.uri(), never self.path(): the default payload can carry
        # the tree-node identifier and the drop then fails with "not a
        # valid or recognized data source". Same trap BasemapItem hit.
        u.uri = self.uri()
        return [u]


class PmtilesTileLayerItem(QgsLayerItem):
    """A PMTiles-backed tile_layer, drawn through the portal's XYZ route.

    The archive itself is unopenable by GDAL when it holds raster tiles
    (its PMTiles driver is vector only), so the portal unpacks tiles
    server-side and this points at that route instead of the file.

    Unlike its COG sibling this needs no GDAL header plumbing: XYZ
    requests go through QNetworkRequest, so a plain ``authcfg`` on the
    URI is applied by QGIS and private and org layers authenticate on
    their own.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        envelope = data or {}
        # Public items deliberately carry no authcfg so a saved project
        # keeps rendering for viewers who never signed in, matching the
        # rule the vector sublayers follow.
        authcfg = "" if item.access == "public" else profile.layer_authcfg_id
        uri = tile_layer_xyz_uri(
            profile.portal_url,
            item.id,
            authcfg_id=authcfg,
            min_zoom=_int_or(envelope.get("minZoom"), 0),
            max_zoom=_int_or(envelope.get("maxZoom"), 18),
            extent=item.bbox,
        )
        super().__init__(
            parent,
            item.title,
            f"gratisgis-tile-layer:/{profile.name}/{item.id}",
            uri,
            _LAYER_TYPE_RASTER,
            "wms",
        )
        self._item = item

    @property
    def item(self) -> ItemSummary:
        return self._item

    def mimeUris(self) -> list[QgsMimeDataUtils.Uri]:
        u = QgsMimeDataUtils.Uri()
        u.layerType = "raster"
        # XYZ rasters are served by the wms provider in QGIS.
        u.providerKey = "wms"
        u.name = self._item.title
        u.uri = self.uri()
        return [u]


def _int_or(value: object, fallback: int) -> int:
    """Coerce a JSON number to int, falling back on anything odd."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return fallback
    return fallback


class UnsupportedTileLayerItem(QgsDataItem):
    """A tile_layer QGIS cannot open, shown but not draggable.

    PMTiles-backed tile_layers hold PNG raster tiles. GDAL's PMTiles
    driver reads vector archives only and rejects these with "Tile type
    PNG not handled by the driver", and the portal has no server-side
    XYZ route to fall back on, so there is no URI that would work.

    Surfacing the item as a plain row with an explanation is the honest
    option: the previous behaviour handed QGIS a layer that failed
    silently, which reads as a broken plugin rather than an unsupported
    format.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        item: ItemSummary,
        *,
        reason: str,
    ) -> None:
        super().__init__(
            _BROWSER_TYPE_NO_TYPE,
            parent,
            item.title,
            f"gratisgis-tile-layer-unsupported:/{item.id}",
        )
        self.setToolTip(reason)
        self.setState(_POPULATED_STATE)


class BasemapItem(QgsLayerItem):
    """A portal `basemap` item. Drag adds an XYZ raster layer using
    the tile URL stored on item.data.

    Basemap data envelope shape (from packages/shared-types):
      { "kind": "tile-url", "tileUrl": "https://.../{z}/{y}/{x}",
        "attribution": "..." }

    The item-list endpoint returns only ItemSummary (no data), so
    the full envelope has to be fetched separately. The fetch
    happens in the PARENT'S createChildren (``_make_item``, on the
    Browser worker thread), never here: a network call inside a
    tree-item constructor would run on whatever thread instantiates
    the node, and basemap leaves used to stall the GUI for one HTTP
    round-trip each on expand.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
        *,
        data: dict[str, object] | None,
    ) -> None:
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
            _LAYER_TYPE_RASTER,
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
        full = get_item(self._profile, self._item.id) or {}
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
                    base_url=base_url,
                    layer_id=None,
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
            children.append(
                _ServiceSublayerItem(
                    self,
                    self._profile,
                    self._item,
                    label=label,
                    base_url=base_url,
                    layer_id=layer_id,
                    is_feature_server=is_feature_server,
                )
            )
        if not children:
            return [_MessageItem(self, "No sublayers found on this service.")]
        return children


class _ServiceSublayerItem(QgsLayerItem):
    """One ArcGIS REST sublayer leaf under a ServiceItem.

    URI construction differs by service type:

      - FeatureServer/N is a first-class endpoint that returns
        features for layer N. QGIS's arcgisfeatureserver provider
        takes the full ``<baseUrl>/<layerId>`` URL.

      - MapServer/N is NOT independently fetchable as a map image;
        only the parent MapServer's ``/export`` endpoint renders
        rasters, and there's no per-layer export. QGIS's
        arcgismapserver provider expects the MapServer ROOT URL
        plus a ``layers='show:N'`` URI key that filters the
        rendered image. Pointing arcgismapserver at /MapServer/N
        directly produces "Network error: Invalid URL" because
        QGIS appends ``/export`` to that path and the server
        rejects the leaf-layer URL.

    ``layer_id`` is None when the sublayer represents the whole
    service (no per-layer metadata to drive a filter); in that
    case we omit the ``layers=`` key and let MapServer return all
    layers / FeatureServer return its default response.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        profile: ConnectionProfile,
        item: ItemSummary,
        *,
        label: str,
        base_url: str,
        layer_id: str | None,
        is_feature_server: bool,
    ) -> None:
        base_root = base_url.rstrip("/")
        if is_feature_server:
            provider = "arcgisfeatureserver"
            layer_type = _LAYER_TYPE_VECTOR
            target_url = (
                f"{base_root}/{layer_id}" if layer_id is not None else base_root
            )
            uri = f"url='{target_url}' crs='EPSG:3857'"
            path_suffix = layer_id if layer_id is not None else "root"
        else:
            provider = "arcgismapserver"
            layer_type = _LAYER_TYPE_RASTER
            # Always point at the MapServer root; filter via layers
            # key. Drop the /N path segment if it slipped in.
            if base_root.rstrip("/").rsplit("/", 1)[-1].isdigit():
                map_root = base_root.rsplit("/", 1)[0]
            else:
                map_root = base_root
            uri = f"url='{map_root}' crs='EPSG:3857'"
            if layer_id is not None:
                uri = f"{uri} layers='show:{layer_id}'"
            path_suffix = layer_id if layer_id is not None else "root"
        super().__init__(
            parent,
            label,
            f"gratisgis-service-sublayer:/{profile.name}/{item.id}/{path_suffix}",
            uri,
            layer_type,
            provider,
        )
        self._profile = profile
        self._item = item
        self._label = label
        self._base_url = base_url
        self._layer_id = layer_id
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
        # Like basemaps, the format lives in the data envelope the list
        # payload omits, and the choice of provider depends on it, so
        # fetch here (parent's createChildren, Browser worker thread)
        # rather than in the leaf constructor.
        full = get_item(profile, item.id) or {}
        raw = full.get("data") if isinstance(full, dict) else None
        data = raw if isinstance(raw, dict) else {}
        # `format` is the field that says what is actually being
        # served, so it alone decides the provider. processingState is
        # NOT a readiness gate: the portal keeps serving a file through
        # every non-terminal state (`tiling` and `building` serve the
        # previous file, `tiling-failed` falls back to the COG), and
        # the state for a finished pyramid is `pmtiles-ready` rather
        # than `ready`. Gating on state == 'ready' therefore hid every
        # PMTiles layer behind a "still being prepared" row while the
        # tiles were sitting there ready to serve.
        fmt = str(data.get("format") or "").lower()
        if fmt == "cog":
            return TileLayerItem(parent, profile, item, data=data)
        if fmt == "pmtiles":
            return PmtilesTileLayerItem(parent, profile, item, data=data)
        # No format means nothing is being served yet, which is the one
        # case worth blocking on. Name the state when we have it so the
        # row says "still uploading" rather than something cryptic.
        state = str(data.get("processingState") or "").lower()
        return UnsupportedTileLayerItem(
            parent,
            item,
            reason=(
                f"This layer is not ready to draw yet (status: {state})."
                if state
                else "This layer has no tile file on the portal yet."
            ),
        )
    if t == "basemap":
        # The tile URL lives in the item's data envelope, which the
        # list payload does not carry. Fetch it HERE: _make_item runs
        # inside the parent group's createChildren, and basemap
        # groups deliberately drop the Fast capability so QGIS calls
        # that on the Browser worker thread. The leaf constructor
        # stays network-free and safe on any thread.
        full = get_item(profile, item.id) or {}
        raw = full.get("data") if isinstance(full, dict) else None
        return BasemapItem(
            parent, profile, item, data=raw if isinstance(raw, dict) else None
        )
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
