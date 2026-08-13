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


def public_ogc_root(portal_url: str) -> str:
    """Return the portal's public OGC API root, no trailing slash."""
    return f"{portal_url.rstrip('/')}/api/public/ogc"


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


def vector_tile_uri(portal_url: str, item_id: str) -> str:
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
    return f"type=xyz&url={template}&zmin=0&zmax=18"


def authed_vector_tile_uri(
    portal_url: str, item_id: str, layer_id: str, *, authcfg_id: str
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
    return f"type=xyz&url={template}&zmin=0&zmax=18&authcfg={authcfg_id}"


# -----------------------------------------------------------
# Reverse parsers (Phase 6 + later)
# -----------------------------------------------------------


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
    if not uri.startswith("type=xyz&url="):
        return None
    template = uri[len("type=xyz&url=") :]
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
