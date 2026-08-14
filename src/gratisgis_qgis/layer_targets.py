# SPDX-License-Identifier: AGPL-3.0-or-later
"""Work out how to put one portal item on the canvas.

The Browser tree already encodes every routing rule the plugin has:
which surface a data_layer sublayer uses, whether an authcfg attaches,
that a tile_layer is a raster whose provider depends on its stored
format, and so on. The Search dock needs the same answers.

It used to carry a second, much smaller copy of those rules, and the
copy drifted: data layers went to the public-only OAPIF surface (so a
private one added from search silently drew nothing), tile layers went
to the vector-tile surface that does not exist for them, and basemaps
were refused outright even though the tree adds them happily.

So this resolves through the tree's own leaf rather than reimplementing
it. One set of rules, and the two entry points cannot disagree again.

Lives outside ``ui`` on purpose: nothing here needs Qt widgets, which
keeps it importable (and testable) without a GUI.
"""
from __future__ import annotations

from dataclasses import dataclass

from gratisgis_client.models.item import ItemSummary

from .settings import ConnectionProfile


@dataclass(frozen=True)
class LayerTarget:
    """What the canvas needs to add one item, or why it cannot."""

    uri: str = ""
    provider: str = ""
    layer_type: str = ""
    name: str = ""
    #: Set instead of a URI when the item exists but is not drawable
    #: (a tile layer still uploading, a private non-spatial table).
    #: The tree shows the same wording as a tooltip.
    message: str = ""

    @property
    def is_drawable(self) -> bool:
        return bool(self.uri)


def resolve_layer_target(
    profile: ConnectionProfile, summary: ItemSummary
) -> LayerTarget | None:
    """Build the tree's leaf for this item and read its layer target.

    Returns None when the item has no Browser representation at all.
    Runs on a worker thread: building the leaf fetches the item's data
    envelope for the types that need one.
    """
    # Imported lazily because browser.items pulls in qgis.core at
    # import time; keeping that out of this module's import graph is
    # what lets it be imported (and tested) on its own.
    from .browser.items import _make_item

    node = _make_item(None, profile, summary)
    if node is None:
        return None

    # Collections (a data_layer with sublayers, a connected service)
    # carry no URI of their own; the first drawable child is what a
    # double click should add.
    children = node.createChildren() if hasattr(node, "createChildren") else []
    for candidate in [node, *children]:
        uris = candidate.mimeUris() if hasattr(candidate, "mimeUris") else []
        if uris:
            first = uris[0]
            return LayerTarget(
                uri=first.uri,
                provider=first.providerKey,
                layer_type=first.layerType,
                name=first.name or summary.title,
            )

    tooltip = node.toolTip() if hasattr(node, "toolTip") else ""
    return LayerTarget(message=str(tooltip or ""))
