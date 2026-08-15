# SPDX-License-Identifier: AGPL-3.0-or-later
"""What QGIS calls on enable and disable, and what must not leak.

``GratisGISPlugin`` is the only class QGIS itself constructs and it had
never been constructed by a test. Everything the plugin installs is
registered globally: a toolbar, menu entries, a Browser provider, two
signal connections on the project, a watchdog thread and a set of log
handlers.

Reload is the case that matters. Plugin Reloader calls unload then
initGui in the same process, so anything not torn down accumulates:
a second toolbar beside the first, a slot bound to a module the reload
has replaced, duplicated log lines, and on Windows an open log handle
that blocks the next in-place upgrade. Each of those has bitten this
plugin at least once.

So the shape of these tests is symmetry: whatever initGui registers,
unload must give back, and doing both twice must leave nothing behind.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub
from tests.plugin.test_browser_items import (
    _StubMimeDataUtils,
    _StubQgsDataCollectionItem,
    _StubQgsDataItem,
    _StubQgsDataItemProvider,
    _StubQgsDataProvider,
    _StubQgsLayerItem,
)


class _Registry:
    """The data-item provider registry, counting what is in it."""

    def __init__(self) -> None:
        self.providers: list[Any] = []

    def addProvider(self, provider: Any) -> None:  # QGIS API name
        self.providers.append(provider)

    def removeProvider(self, provider: Any) -> None:  # QGIS API name
        if provider in self.providers:
            self.providers.remove(provider)


class _Signal:
    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: Any) -> None:
        self.slots.remove(slot)


class _Project:
    singleton: _Project | None = None

    def __init__(self) -> None:
        self.layerWasAdded = _Signal()
        self.readProject = _Signal()

    @classmethod
    def instance(cls) -> _Project:
        assert cls.singleton is not None
        return cls.singleton


class _Toolbar:
    def __init__(self) -> None:
        self.actions: list[Any] = []
        self.object_name = ""
        self.deleted = False

    def setObjectName(self, name: str) -> None:  # Qt API name
        self.object_name = name

    def setToolTip(self, _text: str) -> None:  # Qt API name
        pass

    def addAction(self, action: Any) -> None:  # Qt API name
        self.actions.append(action)

    def addSeparator(self) -> None:  # Qt API name
        self.actions.append("separator")

    def deleteLater(self) -> None:  # Qt API name
        self.deleted = True


class _Iface:
    def __init__(self) -> None:
        self.toolbars: list[_Toolbar] = []
        self.menu_actions: list[Any] = []
        self.docks: list[Any] = []

    def addToolBar(self, _name: str) -> _Toolbar:  # QGIS API name
        bar = _Toolbar()
        self.toolbars.append(bar)
        return bar

    def mainWindow(self) -> Any:  # QGIS API name
        return None

    def addPluginToMenu(self, _menu: str, action: Any) -> None:  # QGIS API
        self.menu_actions.append(action)

    def removePluginMenu(self, _menu: str, action: Any) -> None:  # QGIS API
        if action in self.menu_actions:
            self.menu_actions.remove(action)

    def removeDockWidget(self, dock: Any) -> None:  # QGIS API name
        if dock in self.docks:
            self.docks.remove(dock)


class _Action:
    def __init__(self, _icon: Any, text: str, _parent: Any) -> None:
        self.text = text
        self.triggered = _Signal()

    def setToolTip(self, _text: str) -> None:  # Qt API name
        pass


class _Icon:
    """QIcon, which is constructed both bare and from a file path."""

    def __init__(self, path: str = "", null: bool = False) -> None:
        self.path = path
        self._null = null

    def isNull(self) -> bool:  # Qt API name
        return self._null


@pytest.fixture
def plugin_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _Project.singleton = _Project()
    registry = _Registry()
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsApplication": type(
                    "QgsApplication",
                    (),
                    {
                        "dataItemProviderRegistry": staticmethod(
                            lambda: registry
                        ),
                        "getThemeIcon": staticmethod(lambda _n: _Icon()),
                        "authManager": staticmethod(lambda: None),
                    },
                ),
                "QgsProject": _Project,
                # The browser stubs, not bare types: initGui registers
                # the Browser provider, which imports browser.items,
                # which resolves half a dozen enums off these classes
                # at import time.
                "QgsDataCollectionItem": _StubQgsDataCollectionItem,
                "QgsDataItem": _StubQgsDataItem,
                "QgsDataItemProvider": _StubQgsDataItemProvider,
                "QgsDataProvider": _StubQgsDataProvider,
                "QgsLayerItem": _StubQgsLayerItem,
                "QgsMimeDataUtils": _StubMimeDataUtils,
            },
            "qgis.PyQt.QtCore": {
                "QObject": object,
                "Qt": type(
                    "Qt", (), {"DockWidgetArea": type("A", (), {"RightDockWidgetArea": 2})}
                ),
                "QSettings": type("QSettings", (), {}),
                "QTimer": None,  # forces the watchdog's guarded path
            },
            "qgis.PyQt.QtGui": {"QIcon": _Icon},
            "qgis.PyQt.QtWidgets": {"QAction": _Action},
        },
    )
    import gratisgis_qgis.plugin as m

    m._registry_for_tests = registry  # type: ignore[attr-defined]
    return m


class TestInitGui:
    def test_it_registers_a_toolbar_a_menu_and_a_provider(
        self, plugin_mod: ModuleType
    ) -> None:
        iface = _Iface()
        plugin = plugin_mod.GratisGISPlugin(iface)
        plugin.initGui()

        assert len(iface.toolbars) == 1
        assert iface.menu_actions, "nothing on the Plugins menu"
        assert plugin_mod._registry_for_tests.providers

    def test_the_toolbar_is_named(self, plugin_mod: ModuleType) -> None:
        """Load bearing, not decoration.

        QGIS saves and restores toolbar geometry by object name and
        warns on every start about a toolbar without one.
        """
        iface = _Iface()
        plugin_mod.GratisGISPlugin(iface).initGui()
        assert iface.toolbars[0].object_name

    def test_every_action_is_on_both_the_toolbar_and_the_menu(
        self, plugin_mod: ModuleType
    ) -> None:
        """The toolbar is faster; the menu is where someone looks first.

        The menu is also the one a user cannot accidentally hide.
        """
        iface = _Iface()
        plugin = plugin_mod.GratisGISPlugin(iface)
        plugin.initGui()
        on_toolbar = [
            a for a in iface.toolbars[0].actions if a != "separator"
        ]
        assert len(on_toolbar) == len(iface.menu_actions)

    def test_the_project_signals_are_connected(
        self, plugin_mod: ModuleType
    ) -> None:
        """The extent applier and the load tracer both hang off these."""
        plugin_mod.GratisGISPlugin(_Iface()).initGui()
        assert _Project.instance().layerWasAdded.slots
        assert _Project.instance().readProject.slots


class TestUnloadGivesEverythingBack:
    def _cycle(self, plugin_mod: ModuleType, iface: _Iface) -> Any:
        plugin = plugin_mod.GratisGISPlugin(iface)
        plugin.initGui()
        plugin.unload()
        return plugin

    def test_the_menu_is_emptied(self, plugin_mod: ModuleType) -> None:
        iface = _Iface()
        self._cycle(plugin_mod, iface)
        assert iface.menu_actions == []

    def test_the_toolbar_is_deleted_not_just_emptied(
        self, plugin_mod: ModuleType
    ) -> None:
        """Emptying it is the classic way to end up with five toolbars.

        A reload adds a new one beside the old empty one, and the old
        one stays because QGIS restored its geometry by name.
        """
        iface = _Iface()
        self._cycle(plugin_mod, iface)
        assert iface.toolbars[0].deleted

    def test_the_browser_provider_is_unregistered(
        self, plugin_mod: ModuleType
    ) -> None:
        self._cycle(plugin_mod, _Iface())
        assert plugin_mod._registry_for_tests.providers == []

    def test_the_project_signals_are_disconnected(
        self, plugin_mod: ModuleType
    ) -> None:
        """A slot left bound points into a module the reload replaced.

        The symptom is an exception on the next layer added, from code
        that no longer exists.
        """
        self._cycle(plugin_mod, _Iface())
        assert _Project.instance().layerWasAdded.slots == []
        assert _Project.instance().readProject.slots == []

    def test_a_reload_leaves_exactly_one_of_everything(
        self, plugin_mod: ModuleType
    ) -> None:
        """The case Plugin Reloader actually exercises.

        Two full cycles must look like one, or every reload during a
        working session compounds.
        """
        iface = _Iface()
        for _ in range(3):
            plugin = plugin_mod.GratisGISPlugin(iface)
            plugin.initGui()
            plugin.unload()

        assert iface.menu_actions == []
        assert plugin_mod._registry_for_tests.providers == []
        assert _Project.instance().layerWasAdded.slots == []
        assert all(bar.deleted for bar in iface.toolbars)

    def test_unloading_without_initgui_is_safe(
        self, plugin_mod: ModuleType
    ) -> None:
        """QGIS calls unload on a plugin whose enable failed part way."""
        plugin_mod.GratisGISPlugin(_Iface()).unload()

    def test_unloading_twice_is_safe(self, plugin_mod: ModuleType) -> None:
        plugin = plugin_mod.GratisGISPlugin(_Iface())
        plugin.initGui()
        plugin.unload()
        plugin.unload()


class TestThemeIcons:
    def test_a_missing_theme_icon_falls_back_instead_of_vanishing(
        self, plugin_mod: ModuleType
    ) -> None:
        """A null icon ships as an invisible toolbar button, not an error.

        Theme icon names are renamed and retired between QGIS releases,
        so the fallback is what stops a rename becoming a blank gap in
        the toolbar. The smoke test asserts the names still resolve, so
        this stays theoretical.
        """

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(
                plugin_mod.QgsApplication,
                "getThemeIcon",
                staticmethod(lambda _n: _Icon(null=True)),
            )
            fallback = _Icon()
            assert plugin_mod._theme_icon("/gone.svg", fallback) is fallback
        finally:
            monkeypatch.undo()

    def test_an_icon_lookup_that_raises_falls_back_too(
        self, plugin_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            plugin_mod.QgsApplication,
            "getThemeIcon",
            staticmethod(lambda _n: (_ for _ in ()).throw(RuntimeError("no"))),
        )
        fallback = _Icon()
        assert plugin_mod._theme_icon("/x.svg", fallback) is fallback
