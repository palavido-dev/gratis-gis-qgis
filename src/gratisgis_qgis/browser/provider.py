# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-item provider that QGIS calls to populate the Browser
panel's top-level tree.

Registered at plugin load (`plugin.initGui`) via
`QgsApplication.dataItemProviderRegistry().addProvider(...)` and
unregistered at unload. The provider exposes a single root item
(`RootItem`) that lazy-expands into per-connection children.

QGIS calls `createDataItem(path, parent)` once for the root
(`path == ""`); we don't speak any sub-path scheme because the
Browser tree expansion is driven by `createChildren()` on each
collection item, not by path lookups.
"""
from __future__ import annotations

from qgis.core import (  # type: ignore[import-not-found]
    QgsDataItem,
    QgsDataItemProvider,
    QgsDataProvider,
)

from ..settings import ConnectionStore
from .items import RootItem


class GratisGISDataItemProvider(QgsDataItemProvider):
    """Top-level provider; one instance per plugin load."""

    NAME = "GratisGIS"

    def __init__(self) -> None:
        super().__init__()
        # Single shared store -- safe because QSettings is
        # process-wide and our reads are synchronous.
        self._store = ConnectionStore()

    def name(self) -> str:  # QGIS API name
        return self.NAME

    def capabilities(self) -> int:  # QGIS API name
        # Net browser capabilities: we don't gate on filesystem
        # or database, we're a network-backed provider.
        return QgsDataProvider.Net

    def createDataItem(self, path: str, parent: QgsDataItem | None) -> QgsDataItem | None:
        """Return the root item for the GratisGIS subtree.

        Only the empty path (root) is recognized; sub-paths
        come from the children's `createChildren()` cascade so
        we don't need to parse them here.
        """
        if path:
            return None
        return RootItem(parent, self._store)
