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

from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import QObject  # type: ignore[import-not-found]
from qgis.PyQt.QtGui import QIcon  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import QAction  # type: ignore[import-not-found]

from gratisgis_qgis.log import get_logger

if TYPE_CHECKING:
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
        _log.info("GratisGIS plugin instantiated")

    # ----- QGIS hooks -----

    def initGui(self) -> None:  # QGIS API name
        """Wire up menu and toolbar actions when the plugin is enabled."""
        icon = QIcon()  # Placeholder; resources/icon.svg ships in Phase 1.
        manage_action = QAction(icon, "Manage GratisGIS connections...", self._iface.mainWindow())
        manage_action.triggered.connect(self._on_manage_connections)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, manage_action)
        self._actions.append(manage_action)
        _log.debug("initGui: menu actions registered")

    def unload(self) -> None:
        """Tear down menu and toolbar actions when the plugin is disabled
        or reloaded.
        """
        for action in self._actions:
            self._iface.removePluginMenu(self.PLUGIN_NAME, action)
        self._actions.clear()
        _log.debug("unload: menu actions removed")

    # ----- Action handlers -----

    def _on_manage_connections(self) -> None:
        """Open the connection management dialog."""
        # Lazy import so a load failure surfaces with a clean stack rather
        # than at plugin import time.
        from gratisgis_qgis.ui.connection_dialog import ConnectionManagerDialog

        dlg = ConnectionManagerDialog(self._iface.mainWindow())
        dlg.exec_()
