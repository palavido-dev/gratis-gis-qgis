# SPDX-License-Identifier: AGPL-3.0-or-later
"""GratisGISPlugin: the main plugin class QGIS instantiates.

For Phase 0 the plugin's job is simple:

- Add a "GratisGIS" toolbar action and Plugins menu entry
- Open a connection management dialog from that action
- Wire up the file logger and the QGIS message-panel handler

Phase 1 layers in the Browser panel data provider, item icons,
right-click context menus, and the bridge from the connection store
to the actual portal API client.
"""

from __future__ import annotations

import os.path
from typing import TYPE_CHECKING

from qgis.core import QgsApplication  # type: ignore[import-not-found]
from qgis.PyQt.QtCore import QObject  # type: ignore[import-not-found]
from qgis.PyQt.QtGui import QIcon  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import QAction  # type: ignore[import-not-found]

from .log import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsDataItemProvider  # type: ignore[import-not-found]
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)


class GratisGISPlugin(QObject):
    """Top-level plugin class.

    QGIS calls ``initGui`` on plugin enable and ``unload`` on
    disable. Both are required public hooks; everything else is
    instance-private.
    """

    PLUGIN_NAME = "GratisGIS"

    def __init__(self, iface: QgisInterface) -> None:
        super().__init__()
        self._iface = iface
        self._actions: list[QAction] = []
        self._browser_provider: QgsDataItemProvider | None = None
        _log.info("GratisGIS plugin instantiated")

    # ----- QGIS hooks -----

    def initGui(self) -> None:  # QGIS API name
        """Wire up menu and toolbar actions when the plugin is enabled."""
        icon = _load_icon()
        manage_action = QAction(icon, "Manage GratisGIS connections...", self._iface.mainWindow())
        manage_action.triggered.connect(self._on_manage_connections)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, manage_action)
        self._actions.append(manage_action)

        # Phase 1: register the Browser-panel data item provider so
        # configured connections show up as a "GratisGIS" subtree
        # alongside built-in providers (XYZ Tiles, WMS/WMTS, etc.).
        # Lazy import so a stack of QGIS-only imports doesn't
        # explode at plugin-discovery time on a stripped Python
        # install.
        from .browser.provider import GratisGISDataItemProvider

        self._browser_provider = GratisGISDataItemProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self._browser_provider)
        _log.debug("initGui: menu actions + browser provider registered")

    def unload(self) -> None:
        """Tear down menu and toolbar actions when the plugin is disabled
        or reloaded.
        """
        for action in self._actions:
            self._iface.removePluginMenu(self.PLUGIN_NAME, action)
        self._actions.clear()
        if self._browser_provider is not None:
            QgsApplication.dataItemProviderRegistry().removeProvider(self._browser_provider)
            self._browser_provider = None
        _log.debug("unload: menu actions + browser provider removed")

    # ----- Action handlers -----

    def _on_manage_connections(self) -> None:
        """Open the connection management dialog."""
        # Lazy import so a load failure surfaces with a clean stack rather
        # than at plugin import time.
        from .ui.connection_dialog import ConnectionManagerDialog

        dlg = ConnectionManagerDialog(self._iface.mainWindow())
        dlg.exec_()


def _load_icon() -> QIcon:
    """Locate the plugin's brand icon at `resources/icon.svg`. Falls
    back to an empty QIcon (no graphic) when the file is missing so
    a stripped install doesn't crash the plugin load.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    svg_path = os.path.join(here, "resources", "icon.svg")
    if os.path.isfile(svg_path):
        return QIcon(svg_path)
    return QIcon()
