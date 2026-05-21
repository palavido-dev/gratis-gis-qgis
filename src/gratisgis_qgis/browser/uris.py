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
    """
    return f"url='{public_ogc_root(portal_url)}' typename='{item_id}'"


def vector_tile_uri(portal_url: str, item_id: str) -> str:
    """Build the XYZ vector-tile URI for a portal tile_layer / MVT
    tileset.

    QGIS's vector-tile provider accepts a `type=xyz&url=<template>`
    URI where the template carries `{z}/{y}/{x}` placeholders.
    Our public Tiles endpoint serves `{tileMatrix}/{tileRow}/
    {tileCol}` which maps to z/y/x; matching the QGIS template
    placeholders saves a per-fetch rewrite step.
    """
    base = public_ogc_root(portal_url)
    template = (
        f"{base}/collections/{item_id}"
        "/tiles/WebMercatorQuad/{z}/{y}/{x}"
    )
    return f"type=xyz&url={template}"
