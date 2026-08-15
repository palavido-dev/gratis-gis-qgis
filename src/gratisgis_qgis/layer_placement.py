# SPDX-License-Identifier: AGPL-3.0-or-later
"""Carry a layer's styling and its place in the tree across a replace.

Overwriting an offline clone has to remove the old layer first: Windows
refuses to replace a file another handle still has open, and the usual
holder is the previous clone of the same name sitting in the very
project doing the overwriting. Removing it releases the lock, and the
new file is then loaded as a fresh layer.

A fresh layer is the problem. It comes back with default symbology, at
the bottom of the layer list, outside whatever group it used to live
in. For anyone who had styled a clone, an overwrite quietly threw that
work away, which is worse than refusing the overwrite would have been:
the file updated, so it looked like it worked.

So the layer is photographed before it goes and the photograph is
applied to its replacement. Three things travel:

- **Symbology**, via QGIS's own named-style XML. Using the platform's
  serialisation rather than copying renderer objects means categorised
  renderers, data-defined properties, labelling and blend modes all
  come along without this module knowing what any of them are.
- **Group membership**, as the path of group names from the root.
  Names rather than object references, because the old node is
  destroyed with the layer and a reference to it would be dangling by
  the time it is needed.
- **Position** within that group, so the layer does not jump to the
  bottom of a carefully ordered stack.

Every step is best effort and none of it can fail the clone. Losing
symbology is a nuisance; failing an overwrite that has already written
the file would leave the project pointing at data that no longer
matches what it says.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .log import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class LayerPlacement:
    """How a layer looked and where it sat, before it was replaced."""

    style_xml: str | None = None
    """QGIS named-style XML, or None when it could not be read."""

    group_path: list[str] = field(default_factory=list)
    """Group names from the root down. Empty means top level."""

    index: int = -1
    """Position among its siblings. -1 means "put it wherever"."""

    @property
    def is_empty(self) -> bool:
        """True when nothing worth restoring was captured."""
        return self.style_xml is None and not self.group_path and self.index < 0


def capture_placement(layer: Any) -> LayerPlacement:
    """Photograph a layer before it is removed. Never raises."""
    return LayerPlacement(
        style_xml=_export_style(layer),
        group_path=_group_path(layer),
        index=_index_in_parent(layer),
    )


def restore_placement(layer: Any, placement: LayerPlacement) -> None:
    """Apply a photograph to the replacement layer. Never raises.

    Style first, then position: moving a node in the tree replaces the
    node object, so applying the style afterwards would be styling a
    layer through a reference the move has already invalidated.
    """
    if placement.is_empty:
        return
    _import_style(layer, placement.style_xml)
    _restore_position(layer, placement)


def _export_style(layer: Any) -> str | None:
    """The layer's styling as QGIS's own named-style XML.

    An in-memory document rather than a temp file: the file version
    needs a writable path and a cleanup, and this runs in the middle of
    an overwrite that is already juggling file handles on Windows.
    """
    try:
        from qgis.PyQt.QtXml import QDomDocument  # type: ignore[import-not-found]

        doc = QDomDocument()
        # Signature differs across builds: some return (doc, errorMsg),
        # some take the message by reference. Ask for the simple form
        # and fall back rather than pinning one.
        try:
            layer.exportNamedStyle(doc)
        except TypeError:
            layer.exportNamedStyle(doc, "")
        xml = doc.toString()
    except Exception:
        _log.debug("could not export the layer style", exc_info=True)
        return None
    return xml if isinstance(xml, str) and xml.strip() else None


def _import_style(layer: Any, style_xml: str | None) -> bool:
    """Apply named-style XML to a layer. False when it did not take."""
    if not style_xml:
        return False
    try:
        from qgis.PyQt.QtXml import QDomDocument  # type: ignore[import-not-found]

        doc = QDomDocument()
        if not doc.setContent(style_xml):
            _log.debug("captured style XML would not parse back")
            return False
        result = layer.importNamedStyle(doc)
        # Returns bool on some builds, (bool, message) on others.
        ok = result[0] if isinstance(result, tuple) else result
        if ok:
            layer.triggerRepaint()
        else:
            _log.debug("QGIS declined the captured style")
        return bool(ok)
    except Exception:
        _log.debug("could not restore the layer style", exc_info=True)
        return False


def _tree_node(layer: Any) -> Any:
    from qgis.core import QgsProject  # type: ignore[import-not-found]

    return QgsProject.instance().layerTreeRoot().findLayer(layer.id())


def _group_path(layer: Any) -> list[str]:
    """Names of the groups containing this layer, root first.

    Names, not node references: the node dies with the layer, so a
    reference would be dangling by the time the replacement needs it.
    """
    try:
        node = _tree_node(layer)
        if node is None:
            return []
        path: list[str] = []
        parent = node.parent()
        while parent is not None and parent.parent() is not None:
            path.append(str(parent.name()))
            parent = parent.parent()
        path.reverse()
        return path
    except Exception:
        _log.debug("could not read the layer's group path", exc_info=True)
        return []


def _index_in_parent(layer: Any) -> int:
    try:
        node = _tree_node(layer)
        if node is None:
            return -1
        parent = node.parent()
        if parent is None:
            return -1
        return list(parent.children()).index(node)
    except Exception:
        _log.debug("could not read the layer's position", exc_info=True)
        return -1


def _restore_position(layer: Any, placement: LayerPlacement) -> None:
    """Move the layer back to its group and index. Never raises."""
    try:
        from qgis.core import (  # type: ignore[import-not-found]
            QgsLayerTreeLayer,
            QgsProject,
        )

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        target = _find_group(root, placement.group_path)
        if target is None:
            return
        node = root.findLayer(layer.id())
        if node is None:
            return
        parent = node.parent()
        if parent is target and (
            placement.index < 0
            or list(parent.children()).index(node) == placement.index
        ):
            return
        # Insert a clone at the wanted spot, then drop the original
        # node. QgsLayerTreeNode cannot be reparented in place, and
        # removing first would briefly leave the layer out of the tree.
        clone = QgsLayerTreeLayer(layer)
        index = placement.index
        if index < 0 or index > len(target.children()):
            index = len(target.children())
        target.insertChildNode(index, clone)
        if parent is not None:
            parent.removeChildNode(node)
    except Exception:
        _log.debug("could not restore the layer's position", exc_info=True)


def _find_group(root: Any, path: list[str]) -> Any:
    """Walk a group path from the root, or None if it is gone.

    None rather than recreating missing groups: the user may have
    deleted or renamed one while the clone was downloading, and
    conjuring it back would be the plugin overruling that.
    """
    node = root
    for name in path:
        found = None
        for child in node.children():
            if (
                hasattr(child, "children")
                and str(getattr(child, "name", lambda: "")()) == name
            ):
                found = child
                break
        if found is None:
            return None
        node = found
    return node
