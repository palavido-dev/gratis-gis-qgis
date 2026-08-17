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
        self._processing_provider: object | None = None
        self._layer_actions: list = []
        self._search_dock: GratisGISSearchDock | None = None
        self._toolbar = None
        # Typed as object so the annotation needs no QGIS-only import
        # at plugin-discovery time; see the lazy import in initGui.
        self._extent_applier: object | None = None
        self._load_tracer: object | None = None
        self._freeze_watchdog: object | None = None
        _log.info("GratisGIS plugin instantiated")

    # ----- QGIS hooks -----

    def initGui(self) -> None:  # QGIS API name
        """Wire up the toolbar, menu and browser provider."""
        icon = _load_icon()

        # A toolbar of its own rather than only a menu. Menu entries are
        # several clicks deep and give no room to group anything; a
        # toolbar puts the common actions one click away and leaves
        # somewhere obvious to hang related ones as the plugin grows.
        #
        # setObjectName is load bearing, not decoration: QGIS saves and
        # restores toolbar geometry by object name, and warns on every
        # start about a toolbar that has none.
        self._toolbar = self._iface.addToolBar(self.PLUGIN_NAME)
        self._toolbar.setObjectName("GratisGISToolbar")
        self._toolbar.setToolTip("GratisGIS")

        # Order is the order of use: connect, find something, then act
        # on it. The separators group those three phases.
        #
        # A distinct icon each, because six copies of the plugin logo
        # is a row of identical buttons and no toolbar at all. The
        # icons are the plugin's own, drawn in the portal's palette so
        # the toolbar reads as GratisGIS rather than as six borrowed
        # QGIS buttons. The trade, accepted deliberately: unlike theme
        # icons they do not recolor under Night Mapping. Each carries a
        # theme-icon name as its fallback so a file missing from a
        # stripped install degrades to a visible stock button, never a
        # blank one.
        for label, handler, brand_name, theme_name in (
            ("Manage GratisGIS connections...", self._on_manage_connections,
             "connect.svg", "/mIconConnect.svg"),
            ("Open GratisGIS search...", self._on_open_search,
             "search.svg", "/search.svg"),
            (None, None, None, None),
            ("Publish layer to GratisGIS...", self._on_publish_vector,
             "publish-layer.svg", "/mActionSharingExport.svg"),
            ("Publish current project as GratisGIS map...",
             self._on_publish_project,
             "publish-map.svg", "/mActionSaveMapAsImage.svg"),
            ("Open GratisGIS map in QGIS...", self._on_open_map,
             "open-map.svg", "/mActionFileOpen.svg"),
            (None, None, None, None),
            ("Clone layer for offline use...", self._on_clone_offline,
             "clone.svg", "/mActionDuplicateLayer.svg"),
            ("Sync layer with GratisGIS...", self._on_push_edits,
             "sync.svg", "/mActionRefresh.svg"),
        ):
            if label is None:
                self._toolbar.addSeparator()
                continue
            self._add_action(
                _brand_icon(brand_name, _theme_icon(theme_name, icon)),
                label,
                handler,
            )

        # Phase 1: register the Browser-panel data item provider so
        # configured connections show up as a "GratisGIS" subtree
        # alongside built-in providers (XYZ Tiles, WMS/WMTS, etc.).
        # Lazy import so a stack of QGIS-only imports doesn't
        # explode at plugin-discovery time on a stripped Python
        # install.
        from .browser.provider import GratisGISDataItemProvider

        self._browser_provider = GratisGISDataItemProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self._browser_provider)

        # Processing provider: publish and clone as algorithms, which
        # is what makes them batchable and Model Designer material.
        # Guarded because a build without Processing (or an import
        # error inside it) must cost the algorithms, not the plugin.
        try:
            from .processing import GratisGISProcessingProvider

            self._processing_provider = GratisGISProcessingProvider()
            QgsApplication.processingRegistry().addProvider(
                self._processing_provider
            )
        except Exception:
            self._processing_provider = None
            _log.exception("Processing provider could not be registered")

        # Layers-panel context menu: the portal actions on the object
        # people actually right-click. Grouped under one GratisGIS
        # submenu so three entries do not sprawl across the menu.
        # Guarded like the Processing provider: a QGIS build where the
        # custom-action API moved costs the menu, never the plugin.
        try:
            self._register_layer_actions()
        except Exception:
            _log.exception("layer context actions could not be registered")

        # Portal layers carry their real extent in the layer URI, but a
        # tiled layer reports the whole world until something applies
        # it. Listening on the project catches every route a layer can
        # arrive by, including being dragged out of the Browser tree,
        # where QGIS builds the layer itself and no plugin code runs.
        from .layer_extent import ExtentApplier

        self._extent_applier = ExtentApplier()
        self._extent_applier.install()

        # Diagnostics for the project-load freeze. Installed last, so a
        # failure in either costs only the diagnostic and never the
        # plugin, and installed at all because the freeze's defining
        # feature is that the log had nothing to say about it.
        #
        # The tracer names the layers as they arrive; the watchdog
        # notices the GUI thread has stopped and dumps every Python
        # stack. Together they turn a Task Manager kill into a report.
        from .load_trace import LoadTracer

        self._load_tracer = LoadTracer()
        self._load_tracer.install()

        from .freeze_watch import FreezeWatchdog
        from .log import log_directory

        self._freeze_watchdog = FreezeWatchdog(log_directory())
        self._freeze_watchdog.start()

        _log.debug("initGui: menu actions + browser provider registered")

    def _add_action(self, icon, label: str, handler) -> None:
        """Put one action on both the toolbar and the Plugins menu.

        Both, deliberately. The toolbar is faster once you know the
        plugin; the menu is where someone looks the first time, and is
        the only one of the two a user cannot accidentally hide.
        """
        action = QAction(icon, label, self._iface.mainWindow())
        action.triggered.connect(handler)
        action.setToolTip(label.rstrip("."))
        self._iface.addPluginToMenu(self.PLUGIN_NAME, action)
        if self._toolbar is not None:
            self._toolbar.addAction(action)
        self._actions.append(action)

    def unload(self) -> None:
        """Tear down menu and toolbar actions when the plugin is disabled
        or reloaded.
        """
        for action in self._actions:
            self._iface.removePluginMenu(self.PLUGIN_NAME, action)
        self._actions.clear()
        if self._toolbar is not None:
            # Deleted, not just emptied: a reload would otherwise leave
            # the old toolbar behind and add a second one beside it,
            # which is the classic way a plugin ends up with five.
            self._toolbar.deleteLater()
            self._toolbar = None
        if self._browser_provider is not None:
            QgsApplication.dataItemProviderRegistry().removeProvider(self._browser_provider)
            self._browser_provider = None
        for action in getattr(self, "_layer_actions", []):
            try:
                self._iface.removeCustomActionForLayerType(action)
            except Exception:  # pragma: no cover - defensive
                _log.debug("layer action removal failed", exc_info=True)
        self._layer_actions = []
        if getattr(self, "_processing_provider", None) is not None:
            try:
                QgsApplication.processingRegistry().removeProvider(
                    self._processing_provider
                )
            except Exception:  # pragma: no cover - defensive
                _log.exception("Processing provider removal failed")
            self._processing_provider = None
        if self._extent_applier is not None:
            # Must disconnect: a reload would otherwise leave this
            # instance's slot bound to a module the reload replaced.
            self._extent_applier.remove()  # type: ignore[attr-defined]
            self._extent_applier = None
        if self._load_tracer is not None:
            self._load_tracer.remove()  # type: ignore[attr-defined]
            self._load_tracer = None
        if self._freeze_watchdog is not None:
            # Before teardown_logging below: the watcher thread logs,
            # and stopping it first means it cannot be mid-log while
            # the handlers it is writing to are being closed.
            self._freeze_watchdog.stop()  # type: ignore[attr-defined]
            self._freeze_watchdog = None
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

    def _register_layer_actions(self) -> None:
        """Right-click entries in the Layers panel, per layer type.

        Publish and Sync go on ordinary vector layers (a clone is an
        OGR vector, so Sync lands where clones live). Clone goes on
        vector tile layers, which is what portal layers on the canvas
        are. Each entry opens the flow's existing dialog; the dialogs
        keep owning validation, so a right-click on an unsuitable
        layer gets the dialog's own explanation rather than a dead
        menu entry.
        """
        from qgis.core import Qgis, QgsMapLayer  # type: ignore[import-not-found]
        from qgis.PyQt.QtWidgets import QAction  # type: ignore[import-not-found]

        from .qgis_compat import resolve_enum

        scoped = getattr(Qgis, "LayerType", None)
        vector_type = resolve_enum(
            (scoped, "Vector"), (QgsMapLayer, "VectorLayer")
        )
        vector_tile_type = resolve_enum(
            (scoped, "VectorTile"), (QgsMapLayer, "VectorTileLayer")
        )
        main_window = self._iface.mainWindow()
        for label, handler, layer_types in (
            ("Publish to GratisGIS...", self._on_publish_current_layer,
             (vector_type,)),
            ("Sync layer with GratisGIS...", self._on_push_edits,
             (vector_type,)),
            ("Clone layer for offline use...", self._on_clone_offline,
             (vector_tile_type,)),
        ):
            action = QAction(label, main_window)
            action.triggered.connect(handler)
            for layer_type in layer_types:
                self._iface.addCustomActionForLayerType(
                    action, "GratisGIS", layer_type, True
                )
            self._layer_actions.append(action)

    def _on_publish_current_layer(self) -> None:
        """Publish, preselecting the layer that was right-clicked.

        Right-clicking a layer makes it current in the tree view, so
        the current layer IS the clicked one. Falling back to no
        preselection (the dialog's own picker) rather than failing:
        the menu entry must work even if the view API shifts.
        """
        layer_id: str | None = None
        try:
            view = self._iface.layerTreeView()
            current = view.currentLayer() if view is not None else None
            if current is not None:
                layer_id = current.id()
        except Exception:  # pragma: no cover - defensive
            _log.debug("no current layer for publish", exc_info=True)
        from .ui.publish_vector_dialog import PublishLayerDialog

        dlg = PublishLayerDialog(
            self._iface,
            self._iface.mainWindow(),
            preselect_layer_id=layer_id,
        )
        dlg.exec()

    def _on_open_map(self) -> None:
        """Pick one of the portal's maps and open it as a layer stack."""
        from .ui.open_map_dialog import OpenMapDialog

        dlg = OpenMapDialog(self._iface, self._iface.mainWindow())
        dlg.exec()

    def _on_publish_vector(self) -> None:
        """Open the Publish-vector-layer dialog (Phase 3)."""
        from .ui.publish_vector_dialog import PublishLayerDialog

        dlg = PublishLayerDialog(self._iface, self._iface.mainWindow())
        dlg.exec()

    def _on_push_edits(self) -> None:
        """Open the Push-edits dialog (Phase 4)."""
        from .ui.push_edits_dialog import PushEditsDialog

        dlg = PushEditsDialog(self._iface, self._iface.mainWindow())
        dlg.exec()

    def _on_clone_offline(self) -> None:
        """Open the Clone-to-GeoPackage dialog (Phase 7)."""
        from .ui.clone_dialog import CloneToGeoPackageDialog

        dlg = CloneToGeoPackageDialog(self._iface, self._iface.mainWindow())
        dlg.exec()


def _brand_icon(filename: str, fallback: QIcon) -> QIcon:
    """One of the plugin's own toolbar icons, or the fallback.

    The set lives in ``resources/icons/`` and is drawn in the portal's
    palette (deep sage + tan on a 24 grid) so the toolbar reads as
    GratisGIS. The fallback is the matching QGIS theme icon, so an
    icon missing from a stripped install degrades to a visible stock
    button rather than a blank one; the smoke test asserts every
    bundled file exists and loads, so the fallback stays theoretical.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    svg_path = os.path.join(here, "resources", "icons", filename)
    if not os.path.isfile(svg_path):
        _log.info("brand icon %s is missing; using the theme icon", filename)
        return fallback
    icon = QIcon(svg_path)
    if icon.isNull():  # pragma: no cover - defensive
        _log.info("brand icon %s did not load; using the theme icon", filename)
        return fallback
    return icon


def _theme_icon(name: str, fallback: QIcon) -> QIcon:
    """One of QGIS's own icons, or the plugin's own if it is missing.

    Theme icon names are not a stable API: they are renamed and
    retired between QGIS releases, and asking for one that no longer
    exists yields a null icon, which shows up as an invisible toolbar
    button rather than an error. Falling back to the brand icon means
    the worst case is a duplicate-looking button instead of a gap, and
    the smoke test asserts every name still resolves so the fallback
    stays theoretical.
    """
    try:
        icon = QgsApplication.getThemeIcon(name)
    except Exception:  # pragma: no cover - defensive
        _log.debug("theme icon %s could not be loaded", name, exc_info=True)
        return fallback
    if icon is None or icon.isNull():
        _log.info("QGIS has no theme icon named %s; using the plugin icon", name)
        return fallback
    return icon


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
