# SPDX-License-Identifier: AGPL-3.0-or-later
"""URI builders for QGIS data providers.

Each function returns the exact string QGIS's built-in provider
expects when consuming a portal item. The provider names are
matched to QGIS conventions:

  - OAPIF  -> OGC API Features (built-in vector provider)
  - xyz    -> XYZ tiles (used here for MVT vector tiles via the
              built-in vector-tile provider)

These functions are pure (input -> string) so they're unit-
testable without spinning up QGIS, and they're the single source
of truth for both the Browser tree leaves (browser/items.py)
and the Search dock's add-to-canvas action (ui/search_dock.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

# A parameter of our own, carried in the layer URI so the extent
# travels with the layer instead of costing a network round trip when
# QGIS asks for it.
#
# Why the URI at all: tiled layers (vector tiles, XYZ rasters) always
# report the whole world as their extent, because a tile pyramid is
# defined over the whole world whatever it actually contains. Verified
# against real QGIS: no provider parameter changes that, but setExtent()
# after construction does stick. So something has to apply the extent
# afterwards, and that something only gets handed the layer, not the
# portal item it came from. The URI is the only channel that reaches it,
# because a layer dragged from the Browser tree is built by QGIS itself
# from a mime URI.
#
# The URI is a seed, not storage. An unrecognised parameter survives
# into layer.source() intact, but only a vector-tile source keeps it
# across a project save: a raster's source is rewritten by the
# provider's own encoder on the way out, which drops it. Persistence is
# therefore a custom property; see layer_extent.EXTENT_PROPERTY.
#
# The prefix is deliberately not a bare "extent": that is a plausible
# name for a real provider parameter to acquire later, and a collision
# would mean silently feeding a provider our string.
EXTENT_PARAM = "ggextent"


def format_extent(bbox: tuple[float, float, float, float]) -> str:
    """Render a bbox as the comma-separated text both carriers use.

    Coordinates stay in EPSG:4326, the CRS the portal states, so this
    module needs no coordinate machinery and stays importable without
    QGIS. ``repr``-grade formatting keeps full float precision; a
    rounded extent would be visibly off at street zoom.
    """
    return ",".join(repr(float(v)) for v in bbox)


def parse_extent(text: str) -> tuple[float, float, float, float] | None:
    """Read back what ``format_extent`` wrote, or None if unusable."""
    parts = text.split(",")
    if len(parts) != 4:
        return None
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if any(v != v or v in (float("inf"), float("-inf")) for v in values):
        return None
    min_lon, min_lat, max_lon, max_lat = values
    if min_lon > max_lon or min_lat > max_lat:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def extent_suffix(bbox: tuple[float, float, float, float] | None) -> str:
    """Render a bbox as the URI fragment that carries it, or ""."""
    if bbox is None:
        return ""
    return f"&{EXTENT_PARAM}={format_extent(bbox)}"


def parse_extent_suffix(source: str) -> tuple[float, float, float, float] | None:
    """Recover the bbox stamped into a layer source by the builders.

    Returns None for any source without the parameter, which is most of
    them: this runs over every layer added to the project.
    """
    marker = f"&{EXTENT_PARAM}="
    idx = source.find(marker)
    if idx < 0:
        return None
    return parse_extent(source[idx + len(marker) :].split("&", 1)[0])


def public_ogc_root(portal_url: str) -> str:
    """Return the portal's public OGC API root, no trailing slash."""
    return f"{portal_url.rstrip('/')}/api/public/ogc"


def tile_layer_file_root(portal_url: str) -> str:
    """Return the root the portal serves tile_layer files from.

    Also used as the GDAL path prefix that scopes the credential to
    this portal, so a header is never sent anywhere else.
    """
    return f"{portal_url.rstrip('/')}/api/tile-layer"


def tile_layer_xyz_uri(
    portal_url: str,
    item_id: str,
    *,
    authcfg_id: str = "",
    min_zoom: int = 0,
    max_zoom: int = 18,
    extent: tuple[float, float, float, float] | None = None,
) -> str:
    """Build the XYZ raster URI for a tile_layer item, whatever backs it.

    The portal serves individual tiles for every raster at
    ``/api/tile-layer/:id/tiles/{z}/{x}/{y}.png`` (portal 0.9.27 and
    newer), unrolling PMTiles archives and warping COGs server-side.
    Which one backs an item is the portal's business; this builder does
    not need to know.

    XYZ is deliberately the right shape rather than a GDAL source, and
    not only for the auth: QGIS's XYZ tiles go through QNetworkRequest,
    which is where an ``authcfg`` is applied, so private and org layers
    authenticate with no special handling. A GDAL ``/vsicurl`` source
    ignores authcfg entirely AND deadlocks QGIS during project load
    (#24), which is why the plugin no longer builds one.
    """
    template = (
        f"{tile_layer_file_root(portal_url)}/{item_id}/tiles/{{z}}/{{x}}/{{y}}.png"
    )
    uri = f"type=xyz&url={quote(template, safe='')}&zmin={min_zoom}&zmax={max_zoom}"
    if authcfg_id:
        uri = f"{uri}&authcfg={authcfg_id}"
    return f"{uri}{extent_suffix(extent)}"


def tile_layer_cog_uri(portal_url: str, item_id: str) -> str:
    """The LEGACY ``/vsicurl`` source shape. Nothing builds it any more.

    Plugin versions up to 0.10.x added COG-backed rasters this way, so
    the shape survives in saved projects and on canvases, and two
    things still have to recognise it: ``parse_tile_layer_uri`` (so
    publish-as-map does not report such a layer as an unpublishable
    local file) and ``scripts/repair_project.py`` (which rewrites it
    to the tile route, because a project holding one deadlocks QGIS on
    open, #24). The builder is kept so their tests construct the exact
    string the old plugin wrote rather than a hand-typed guess of it.

    Do not wire this into any layer-creation path.
    """
    return f"/vsicurl/{tile_layer_file_root(portal_url)}/{item_id}/file.cog"


def oapif_uri(portal_url: str, item_id: str) -> str:
    """Build the OGC API Features URI for a portal data_layer item.

    Returned in the `key='value' key='value'` shape QGIS's OAPIF
    provider parses. The collection id reuses the portal's
    collection-id scheme: bare UUID for single-layer items,
    `<itemId>__<layerKey>` for multi-layer items. The caller
    passes the already-formed collection id (the items endpoint
    returns it directly), so this builder doesn't have to know
    about the multi-layer split.

    ``restrictToRequestBBOX=1`` tells QGIS's OAPIF provider to
    issue ``bbox=`` requests scoped to the current map viewport
    every time it renders. Without it, the provider pulls
    features in collection order with no spatial filter, which
    on a 1.4M-row layer (WV Parcels) means QGIS fetches a 16 MB
    GeoJSON of arbitrary features and then ignores the user's
    pan/zoom. With the flag set, each render hits the engine's
    bbox-optimised path (sub-3s for a county-scale viewport)
    and zooming refreshes the canvas correctly.

    ``pageSize=1000`` keeps each request's payload bounded; the
    provider follows ``next`` links if it needs more within a
    single viewport. Empirically 1000 is a good balance between
    request-rate and per-request latency for polygon layers.
    """
    return (
        f"url='{public_ogc_root(portal_url)}' "
        f"typename='{item_id}' "
        f"restrictToRequestBBOX='1' "
        f"pageSize='1000'"
    )


def authed_ogc_root(portal_url: str) -> str:
    """The signed-in OGC API Features root (portal 0.9.28+).

    Serves every data layer the caller can read, clipped by their
    share geo limits and row scope server-side; the public root only
    serves public items.
    """
    return f"{portal_url.rstrip('/')}/api/ogc"


def authed_oapif_uri(
    portal_url: str, collection_id: str, *, authcfg_id: str
) -> str:
    """The OGC API Features URI for a NON-public data_layer.

    Same shape and same paging discipline as ``oapif_uri`` (see its
    docstring for why restrictToRequestBBOX and pageSize are load
    bearing), pointed at the signed-in surface with the connection's
    layer authcfg attached. The OAPIF provider is QNetworkRequest
    based, so the authcfg travels on every request, exactly like the
    vector tile and XYZ paths. This is what turns a private layer
    into a TRUE feature layer in QGIS: attribute table, selection,
    the works, not just drawn tiles.
    """
    return (
        f"url='{authed_ogc_root(portal_url)}' "
        f"typename='{collection_id}' "
        f"restrictToRequestBBOX='1' "
        f"pageSize='1000' "
        f"authcfg='{authcfg_id}'"
    )


def vector_tile_uri(
    portal_url: str,
    item_id: str,
    *,
    extent: tuple[float, float, float, float] | None = None,
) -> str:
    """Build the XYZ vector-tile URI for a portal tile_layer / MVT
    tileset.

    QGIS's vector-tile provider accepts a `type=xyz&url=<template>`
    URI where the template carries `{z}/{y}/{x}` placeholders.
    Our public Tiles endpoint serves `{tileMatrix}/{tileRow}/
    {tileCol}` which maps to z/y/x; matching the QGIS template
    placeholders saves a per-fetch rewrite step.

    ``zmin`` / ``zmax`` constrain the zoom range QGIS will fetch.
    Without them QGIS defaults to 0-14 but its layer-extent
    calculation can issue probe requests at every zoom on layer
    add, causing a multi-second stall on huge layers. Bounding
    the range lets QGIS skip wasteful probes and only fetch
    tiles within the range our engine actually generates.
    """
    base = public_ogc_root(portal_url)
    template = (
        f"{base}/collections/{item_id}"
        "/tiles/WebMercatorQuad/{z}/{y}/{x}"
    )
    return f"type=xyz&url={template}&zmin=0&zmax=18{extent_suffix(extent)}"


def authed_vector_tile_uri(
    portal_url: str,
    item_id: str,
    layer_id: str,
    *,
    authcfg_id: str,
    extent: tuple[float, float, float, float] | None = None,
) -> str:
    """Build the authenticated XYZ vector-tile URI for a non-public
    data_layer sublayer.

    Points at the portal's per-layer MVT route
    ``/api/items/:itemId/layers/:layerId/tile/:z/:x/:y.mvt`` with a
    QGIS authcfg id appended, so every tile request carries the
    connection's read-only layer key (the core API Header auth
    method injects ``Authorization: Bearer ggk_...``). This is what
    makes private and org layers actually draw on the canvas; the
    public OGC surface would list them but serve empty tiles.

    Tile-coordinate order is NOT the public builder's: this route is
    ``{z}/{x}/{y}`` (the tile-server convention the portal's own map
    page uses), while the public OGC Tiles surface is ``{z}/{y}/{x}``
    (tileMatrix / tileRow / tileCol). Swapping them fetches the wrong
    tiles everywhere except along the diagonal, which renders as a
    scrambled layer rather than an obvious error, so both orders are
    pinned by tests.
    """
    base = portal_url.rstrip("/")
    template = (
        f"{base}/api/items/{item_id}/layers/{layer_id}/tile"
        "/{z}/{x}/{y}.mvt"
    )
    return (
        f"type=xyz&url={template}&zmin=0&zmax=18&authcfg={authcfg_id}"
        f"{extent_suffix(extent)}"
    )


# -----------------------------------------------------------
# Reverse parsers (Phase 6 + later)
# -----------------------------------------------------------


def uri_param(uri: str, key: str) -> str | None:
    """One ``key=value`` out of a provider URI, wherever it sits.

    Provider URIs are an unordered parameter bag and QGIS rewrites them
    to suit itself. A layer built as
    ``type=xyz&url=...&authcfg=ab12cd3`` comes back from a reloaded
    project as ``authcfg=ab12cd3&type=xyz&url=...``, with the same
    meaning and a different spelling.

    Both parsers below used to key off the string STARTING with
    ``type=xyz``, so a reordered URI stopped being recognised as a
    portal layer at all. The layer still drew; it simply became
    invisible to everything that asks "is this ours", which is
    publish-as-map, the clone picker, the sync picker and the load
    trace. Reported as publish-as-map not recognising a hillshade that
    was plainly sitting in the portal's own tree.

    The value is unquoted, because the URL inside is percent-encoded
    (``quote(safe='')``) precisely so it carries no bare ``&`` and this
    split stays sound.
    """
    for part in uri.split("&"):
        name, sep, value = part.partition("=")
        if sep and name.strip() == key:
            return unquote(value)
    return None


def parse_tile_layer_uri(uri: str) -> tuple[str, str] | None:
    """Recover (portal_url, item_id) from a tile_layer source.

    Inverse of both raster builders, which emit two very different
    strings for the same kind of item:

      - ``tile_layer_cog_uri`` -> ``/vsicurl/<portal>/api/tile-layer/
        <id>/file.cog``, read by GDAL.
      - ``tile_layer_xyz_uri`` -> ``type=xyz&url=<encoded template>``,
        read by the tiled-raster provider.

    Both have to be recognised, because which one a layer got depends
    on how the item was stored, which the user has no visibility of.
    Publish-as-map recognised neither, so a portal raster sitting on
    the canvas was reported as an unpublishable local file.
    """
    if not uri:
        return None
    text = uri.strip()

    # XYZ shape: pull the template out of the parameter bag first,
    # by name rather than by position. Anything without a url
    # parameter is a plain path (the GDAL/vsicurl shape) and is
    # searched as-is.
    from_param = uri_param(text, "url")
    if from_param is not None:
        text = from_param

    # GDAL prefixes stack, and in either order (/vsizip//vsicurl/...),
    # so strip until none is left rather than walking a fixed list
    # once: taking them in list order leaves the second one in place.
    while True:
        for prefix in ("/vsicurl/", "/vsizip/", "/vsigzip/"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        else:
            break

    marker = "/api/tile-layer/"
    index = text.find(marker)
    if index < 0:
        return None
    portal_url = text[:index]
    rest = text[index + len(marker) :]
    item_id = rest.split("/", 1)[0]
    if not portal_url or not item_id:
        return None
    return portal_url, item_id


def parse_oapif_uri(uri: str) -> tuple[str, str] | None:
    """Recover (portal_url, item_id) from an OAPIF source URI.

    Recognises the shape `oapif_uri()` emits. Returns None when
    the URI doesn't carry both `url=` and `typename=` keys, or
    when the URL doesn't end in `/api/public/ogc`. The Phase 6
    publish-project flow walks QGIS layers and uses this to
    figure out which canvas layers map to existing portal items.
    """
    if "url=" not in uri or "typename=" not in uri:
        return None
    url = _parse_quoted_kv(uri, "url")
    typename = _parse_quoted_kv(uri, "typename")
    if not url or not typename:
        return None
    suffix = "/api/public/ogc"
    if not url.endswith(suffix):
        return None
    return url[: -len(suffix)], typename


def parse_vector_tile_uri(uri: str) -> tuple[str, str] | None:
    """Recover (portal_url, collection_id) from a vector-tile XYZ URI.

    Inverse of both tile builders. Recognises the public OGC Tiles
    shape (`vector_tile_uri()`) and the authed per-layer MVT shape
    (`authed_vector_tile_uri()`); for the authed shape the returned
    collection id is the ``<itemId>__<layerId>`` join, matching the
    portal's collection-id convention, so downstream consumers
    (publish-project's layer recognizer) treat both shapes alike.
    Returns None when the URI shape doesn't match either.
    """
    template = uri_param(uri, "url")
    if template is None:
        return None
    marker = "/api/public/ogc/collections/"
    idx = template.find(marker)
    if idx >= 0:
        portal_url = template[:idx]
        rest = template[idx + len(marker) :]
        # rest now looks like "<itemId>/tiles/WebMercatorQuad/{z}/{y}/{x}"
        end = rest.find("/")
        if end < 0:
            return None
        item_id = rest[:end]
        if not item_id:
            return None
        return portal_url, item_id
    return _parse_authed_tile_template(template)


def _parse_authed_tile_template(template: str) -> tuple[str, str] | None:
    """The authed-MVT half of ``parse_vector_tile_uri``.

    ``template`` is everything after ``type=xyz&url=`` (trailing
    ``&zmin=...&authcfg=...`` params included; the id segments end at
    a ``/`` long before those, so they never interfere).
    """
    marker = "/api/items/"
    idx = template.find(marker)
    if idx < 0:
        return None
    portal_url = template[:idx]
    rest = template[idx + len(marker) :]
    # rest now looks like "<itemId>/layers/<layerId>/tile/{z}/{x}/{y}.mvt..."
    parts = rest.split("/")
    if len(parts) < 4 or parts[1] != "layers" or parts[3] != "tile":
        return None
    item_id, layer_id = parts[0], parts[2]
    if not item_id or not layer_id:
        return None
    return portal_url, f"{item_id}__{layer_id}"


# -----------------------------------------------------------
# Portal layer resolution (what the dialogs actually need)
# -----------------------------------------------------------


@dataclass(frozen=True)
class PortalLayerRef:
    """The portal coordinates behind a QGIS layer.

    ``layer_id`` is ``"default"`` for the portal's bare-UUID v1
    collection alias, which addresses an item's first layer without
    naming it. The features endpoints take that spelling, so callers
    never have to special-case the old shape.
    """

    portal_url: str
    item_id: str
    layer_id: str


def parse_portal_layer_source(source: str) -> PortalLayerRef | None:
    """Resolve a QGIS layer source to the portal item + layer it came from.

    The Browser tree emits THREE different URIs for portal layers,
    picked by ``browser/items.py`` from the sublayer's geometry and
    the item's access:

      1. OAPIF, for non-spatial sublayers (``oapif_uri``)
      2. public vector tiles, for spatial sublayers on public items
         (``vector_tile_uri``)
      3. authed per-layer MVT, for spatial sublayers otherwise
         (``authed_vector_tile_uri``)

    Recognising only the first is why the clone dialog reported "no
    portal-backed layers in project" for ordinary spatial data: that
    is the common case and it never travels as OAPIF. Anything that
    walks project layers looking for portal ones must go through this
    function rather than a single-shape parser.

    Returns None for off-portal layers.
    """
    parsed = parse_oapif_uri(source) or parse_vector_tile_uri(source)
    if parsed is None:
        return None
    portal_url, collection_id = parsed
    return _ref_from_collection_id(portal_url, collection_id)


def parse_oapif_layer_source(source: str) -> PortalLayerRef | None:
    """The editable subset of ``parse_portal_layer_source``.

    QGIS's vector-tile layers are a read-only rendering format: they
    have no edit buffer, so a user can never produce edits to push
    from one. The push-edits dialog therefore resolves only the OAPIF
    shape, and offering the tile shapes there would list layers that
    can only ever yield an empty plan.
    """
    parsed = parse_oapif_uri(source)
    if parsed is None:
        return None
    portal_url, collection_id = parsed
    return _ref_from_collection_id(portal_url, collection_id)


def _ref_from_collection_id(portal_url: str, collection_id: str) -> PortalLayerRef:
    """Split a portal collection id into its item and layer parts.

    ``<itemId>__<layerId>`` for v3 multi-layer items; a bare item id
    for the v1 alias, which resolves to the ``default`` layer.
    """
    if "__" in collection_id:
        item_id, layer_id = collection_id.split("__", 1)
        return PortalLayerRef(
            portal_url=portal_url, item_id=item_id, layer_id=layer_id
        )
    return PortalLayerRef(
        portal_url=portal_url, item_id=collection_id, layer_id="default"
    )


def _parse_quoted_kv(uri: str, key: str) -> str | None:
    """Pull `key='value'` out of an OAPIF-style URI string.

    Tolerant of either single or double quotes around the value
    (QGIS emits single quotes by convention but some downstream
    code rewraps with doubles). Returns the unquoted value or
    None when the key isn't present in the expected shape.
    """
    needle = f"{key}="
    idx = uri.find(needle)
    if idx < 0:
        return None
    rest = uri[idx + len(needle) :]
    if not rest:
        return None
    quote = rest[0]
    if quote not in ("'", '"'):
        return None
    end = rest.find(quote, 1)
    if end < 0:
        return None
    return rest[1:end]
