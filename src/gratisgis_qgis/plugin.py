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
from qgis.PyQt.QtCore import QObject, Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtGui import QIcon  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import QAction  # type: ignore[import-not-found]

from .log import get_logger, teardown_logging

if TYPE_CHECKING:
    from qgis.core import QgsDataItemProvider  # type: ignore[import-not-found]
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

    from .ui.search_dock import GratisGISSearchDock

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
        self._search_dock: GratisGISSearchDock | None = None
        # Typed as object so the annotation needs no QGIS-only import
        # at plugin-discovery time; see the lazy import in initGui.
        self._extent_applier: object | None = None
        _log.info("GratisGIS plugin instantiated")

    # ----- QGIS hooks -----

    def initGui(self) -> None:  # QGIS API name
        """Wire up menu and toolbar actions when the plugin is enabled."""
        icon = _load_icon()
        manage_action = QAction(icon, "Manage GratisGIS connections...", self._iface.mainWindow())
        manage_action.triggered.connect(self._on_manage_connections)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, manage_action)
        self._actions.append(manage_action)

        search_action = QAction(icon, "Open GratisGIS search...", self._iface.mainWindow())
        search_action.triggered.connect(self._on_open_search)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, search_action)
        self._actions.append(search_action)

        publish_layer_action = QAction(
            icon, "Publish vector layer to GratisGIS...", self._iface.mainWindow()
        )
        publish_layer_action.triggered.connect(self._on_publish_vector)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, publish_layer_action)
        self._actions.append(publish_layer_action)

        push_edits_action = QAction(
            icon, "Push edits to GratisGIS...", self._iface.mainWindow()
        )
        push_edits_action.triggered.connect(self._on_push_edits)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, push_edits_action)
        self._actions.append(push_edits_action)

        publish_raster_action = QAction(
            icon, "Publish raster / tile layer to GratisGIS...", self._iface.mainWindow()
        )
        publish_raster_action.triggered.connect(self._on_publish_raster)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, publish_raster_action)
        self._actions.append(publish_raster_action)

        clone_action = QAction(
            icon, "Clone layer for offline use...", self._iface.mainWindow()
        )
        clone_action.triggered.connect(self._on_clone_offline)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, clone_action)
        self._actions.append(clone_action)

        publish_action = QAction(
            icon, "Publish current project as GratisGIS map...", self._iface.mainWindow()
        )
        publish_action.triggered.connect(self._on_publish_project)
        self._iface.addPluginToMenu(self.PLUGIN_NAME, publish_action)
        self._actions.append(publish_action)

        # Phase 1: register the Browser-panel data item provider so
        # configured connections show up as a "GratisGIS" subtree
        # alongside built-in providers (XYZ Tiles, WMS/WMTS, etc.).
        # Lazy import so a stack of QGIS-only imports doesn't
        # explode at plugin-discovery time on a stripped Python
        # install.
        from .browser.provider import GratisGISDataItemProvider

        self._browser_provider = GratisGISDataItemProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self._browser_provider)

        # Portal layers carry their real extent in the layer URI, but a
        # tiled layer reports the whole world until something applies
        # it. Listening on the project catches every route a layer can
        # arrive by, including being dragged out of the Browser tree,
        # where QGIS builds the layer itself and no plugin code runs.
        from .layer_extent import ExtentApplier

        self._extent_applier = ExtentApplier()
        self._extent_applier.install()
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
        if self._extent_applier is not None:
            # Must disconnect: a reload would otherwise leave this
            # instance's slot bound to a module the reload replaced.
            self._extent_applier.remove()  # type: ignore[attr-defined]
            self._extent_applier = None
        if self._search_dock is not None:
            self._iface.removeDockWidget(self._search_dock)
            self._search_dock.deleteLater()
            self._search_dock = None
        _log.debug("unload: menu actions + browser provider + search dock removed")
        # Last: release the log handlers so a reload does not stack
        # duplicates onto the persistent stdlib logger (and so the
        # log file is unlocked for in-place upgrades on Windows).
        teardown_logging()

    # ----- Action handlers -----

    def _on_manage_connections(self) -> None:
        """Open the connection management dialog."""
        # Lazy import so a load failure surfaces with a clean stack rather
        # than at plugin import time.
        from .ui.connection_dialog import ConnectionManagerDialog

        dlg = ConnectionManagerDialog(self._iface.mainWindow())
        dlg.exec()
        # The connection list may have changed; refresh the
        # search dock's connection picker so a freshly added
        # profile shows up without restarting the plugin.
        if self._search_dock is not None:
            self._search_dock.refresh_connections()

    def _on_open_search(self) -> None:
        """Show (and create on first call) the search dock widget."""
        if self._search_dock is None:
            from .ui.search_dock import GratisGISSearchDock

            self._search_dock = GratisGISSearchDock(self._iface, self._iface.mainWindow())
            self._iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._search_dock)
        self._search_dock.show()
        self._search_dock.raise_()

    def _on_publish_project(self) -> None:
        """Open the Publish-project-as-map dialog (Phase 6)."""
        # Lazy import keeps the publish module out of the plugin-
        # load critical path; if QGIS or Qt versions diverge from
        # what the dialog expects we want the user to see the error
        # when they click Publish, not when they enable the plugin.
        from .ui.publish_project_dialog import PublishProjectDialog

        dlg = PublishProjectDialog(self._iface, self._iface.mainWindow())
        dlg.exec()

    def _on_publish_vector(self) -> None:
        """Open the Publish-vector-layer dialog (Phase 3)."""
        from .ui.publish_vector_dialog import PublishVectorDialog

        dlg = PublishVectorDialog(self._iface, self._iface.mainWindow())
        dlg.exec()

    def _on_push_edits(self) -> None:
        """Open the Push-edits dialog (Phase 4)."""
        from .ui.push_edits_dialog import PushEditsDialog

        dlg = PushEditsDialog(self._iface, self._iface.mainWindow())
        dlg.exec()

    def _on_publish_raster(self) -> None:
        """Open the Publish-raster dialog (Phase 5)."""
        from .ui.publish_raster_dialog import PublishRasterDialog

        dlg = PublishRasterDialog(self._iface, self._iface.mainWindow())
        dlg.exec()

    def _on_clone_offline(self) -> None:
        """Open the Clone-to-GeoPackage dialog (Phase 7)."""
        from .ui.clone_dialog import CloneToGeoPackageDialog

        dlg = CloneToGeoPackageDialog(self._iface, self._iface.mainWindow())
        dlg.exec()


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
