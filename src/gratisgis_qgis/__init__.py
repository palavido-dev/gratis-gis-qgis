# SPDX-License-Identifier: AGPL-3.0-or-later
"""GratisGIS QGIS plugin entry point.

QGIS loads a plugin by importing the package directory and calling
``classFactory(iface)``, which must return the plugin instance.
"""

from __future__ import annotations


def classFactory(iface):  # type: ignore[no-untyped-def]  # QGIS API name
    """Return the plugin instance.

    Lazy-import the actual plugin class so a syntax error or missing
    dependency in ``plugin.py`` doesn't prevent QGIS from at least
    surfacing the load error in its UI.
    """
    from .plugin import GratisGISPlugin

    return GratisGISPlugin(iface)
