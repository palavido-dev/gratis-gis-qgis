# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-version QGIS / Qt compatibility helpers.

Most Qt enum drift (Qt 5 unscoped vs Qt 6 scoped) is handled
inline by using the scoped form (e.g. ``Qt.ItemFlag.ItemIsSelectable``),
which works on both PyQt5 and PyQt6.

What needs a real shim is layer-type detection: QGIS 4 retired
``QgsMapLayer.VectorLayer`` (an int constant on the class) in
favor of ``Qgis.LayerType.Vector`` (a scoped enum), and
``layer.type()`` returns the new enum. ``isinstance`` against the
concrete subclass works identically on both, so that's what the
helpers below use.
"""
from __future__ import annotations

from typing import Any


def is_vector_layer(layer: Any) -> bool:
    """True iff ``layer`` is a QGIS vector layer."""
    if layer is None:
        return False
    try:
        from qgis.core import QgsVectorLayer  # type: ignore[import-not-found]

        return isinstance(layer, QgsVectorLayer)
    except ImportError:  # pragma: no cover
        return False


def is_raster_layer(layer: Any) -> bool:
    """True iff ``layer`` is a QGIS raster layer."""
    if layer is None:
        return False
    try:
        from qgis.core import QgsRasterLayer  # type: ignore[import-not-found]

        return isinstance(layer, QgsRasterLayer)
    except ImportError:  # pragma: no cover
        return False
