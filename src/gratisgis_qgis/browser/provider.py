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

from ..qgis_compat import resolve_enum
from ..settings import ConnectionStore
from .items import RootItem

try:
    from qgis.core import Qgis  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover (stubbed test runs)
    Qgis = None  # type: ignore[assignment]

# QGIS 3 typed data-item-provider capabilities as class-level
# QgsDataProvider shortcuts (Net = network-backed); QGIS 3.36
# introduced the scoped Qgis.DataItemProviderCapability enum
# (NetworkSources) and QGIS 4 under strict PyQt6 drops the old
# shortcut. Same numeric value (1 << 3) in every home, so whichever
# resolves first is interchangeable at the C++ boundary.
_NET_CAPABILITY = resolve_enum(
    (
        getattr(Qgis, "DataItemProviderCapability", None) if Qgis else None,
        "NetworkSources",
    ),
    (getattr(QgsDataProvider, "DataCapability", None), "Net"),
    (QgsDataProvider, "Net"),
)


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

    def capabilities(self):  # QGIS API name
        # Net browser capabilities: we don't gate on filesystem
        # or database, we're a network-backed provider.
        return _NET_CAPABILITY

    def createDataItem(self, path: str, parent: QgsDataItem | None) -> QgsDataItem | None:
        """Return the root item for the GratisGIS subtree.

        Only the empty path (root) is recognized; sub-paths
        come from the children's `createChildren()` cascade so
        we don't need to parse them here.
        """
        if path:
            return None
        return RootItem(parent, self._store)
