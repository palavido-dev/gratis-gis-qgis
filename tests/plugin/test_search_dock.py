# SPDX-License-Identifier: AGPL-3.0-or-later
"""The search dock: what it shows, and where it sends a double-click.

Test 1 in the playbook and the first surface a new user touches, at 0%
covered until now. ``GratisGISSearchDock`` had never been constructed
by anything.

Two things here carry real risk and neither is about widgets. Adding a
result to the canvas has to route by layer type, and the wrong iface
call is a layer that silently does not appear rather than an error.
And two searches can be in flight at once, where the older one must not
land on top of the newer one's results.

Methods are exercised on a dock built with ``__new__``: they touch a
handful of named attributes and no widget tree, and constructing the
real thing would be testing Qt. Every attribute the code under test
reads is set explicitly, so a rename fails here rather than being
quietly skipped.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from gratisgis_client.models.item import ItemSummary
from tests.plugin.conftest import install_qgis_stub

_WIDGETS = [
    "QComboBox", "QDockWidget", "QHBoxLayout", "QLabel", "QLineEdit",
    "QListWidget", "QListWidgetItem", "QMessageBox", "QPushButton",
    "QVBoxLayout", "QWidget",
]


class _StubQt:
    class ItemDataRole:
        UserRole = 32


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.PyQt.QtCore": {
                "Qt": _StubQt,
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": {n: type(n, (), {}) for n in _WIDGETS},
        },
    )
    import gratisgis_qgis.ui.search_dock as m

    return m


_NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


def _item(**overrides: Any) -> ItemSummary:
    base: dict[str, Any] = {
        "id": "item-1",
        "type": "data_layer",
        "title": "Parcels",
        "access": "private",
        "owner_id": "user-1",
        "org_id": "org-1",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return ItemSummary(**base)


class TestResultFormatting:
    def test_a_row_says_what_the_item_is_and_who_can_see_it(
        self, mod: ModuleType
    ) -> None:
        row = mod._format_result_row(_item())
        assert "Parcels" in row and "data_layer" in row and "private" in row

    def test_the_tooltip_carries_the_optional_fields_when_present(
        self, mod: ModuleType
    ) -> None:
        tip = mod._format_tooltip(
            _item(description="County parcels", tags=["parcels", "wv"])
        )
        assert "County parcels" in tip
        assert "parcels, wv" in tip
        assert "2026-08-15" in tip

    def test_the_tooltip_omits_absent_fields_rather_than_showing_blanks(
        self, mod: ModuleType
    ) -> None:
        """An empty "Tags:" line reads as "this item has a tags problem"."""
        tip = mod._format_tooltip(_item())
        assert "Tags:" not in tip
        assert tip.count("\n\n") == 0


class TestTypeFilter:
    def test_every_offered_filter_maps_to_something(
        self, mod: ModuleType
    ) -> None:
        """The first entry is "any"; the rest name a real item type."""
        assert mod._TYPE_FILTERS
        assert mod._TYPE_FILTERS[0][1] is None
        assert all(label for label, _ in mod._TYPE_FILTERS)

    @pytest.mark.parametrize("idx", [-1, 999])
    def test_an_out_of_range_index_means_no_filter(
        self, mod: ModuleType, idx: int
    ) -> None:
        """A combo with no selection reports -1, and must not IndexError.

        Reachable on first show, before anything is selected.
        """
        dock = mod.GratisGISSearchDock.__new__(mod.GratisGISSearchDock)
        dock._type_combo = SimpleNamespace(currentIndex=lambda: idx)
        assert dock._selected_type() is None

    def test_a_valid_index_returns_that_type(self, mod: ModuleType) -> None:
        dock = mod.GratisGISSearchDock.__new__(mod.GratisGISSearchDock)
        dock._type_combo = SimpleNamespace(currentIndex=lambda: 1)
        assert dock._selected_type() == mod._TYPE_FILTERS[1][1]


class _FakeList:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def addItem(self, row: Any) -> None:  # Qt API name
        self.rows.append(row)

    def clear(self) -> None:  # Qt API name
        self.rows.clear()


class TestRenderResults:
    def _dock(self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Any:
        class _Row:
            def __init__(self, text: str) -> None:
                self.text = text
                self.payload: Any = None
                self.tooltip = ""

            def setData(self, _role: Any, value: Any) -> None:  # Qt API
                self.payload = value

            def setToolTip(self, text: str) -> None:  # Qt API
                self.tooltip = text

        monkeypatch.setattr(mod, "QListWidgetItem", _Row)
        dock = mod.GratisGISSearchDock.__new__(mod.GratisGISSearchDock)
        dock._results = _FakeList()
        dock._status = SimpleNamespace(
            text="", setText=lambda t: setattr(dock._status, "text", t)
        )
        return dock

    def test_results_are_sorted_case_insensitively(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise every lowercase title sinks below every uppercase one."""
        dock = self._dock(mod, monkeypatch)
        dock._render_results([
            _item(id="1", title="zebra"),
            _item(id="2", title="Apple"),
            _item(id="3", title="mango"),
        ])
        assert [r.text.split("  -")[0] for r in dock._results.rows] == [
            "Apple", "mango", "zebra"
        ]

    def test_items_qgis_cannot_open_are_left_out(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same filter the Browser tree uses.

        A dashboard in the results has no add-to-canvas action, so
        double-clicking it can only disappoint.
        """
        dock = self._dock(mod, monkeypatch)
        dock._render_results([
            _item(id="1", title="Parcels", type="data_layer"),
            _item(id="2", title="Ops board", type="dashboard"),
        ])
        assert len(dock._results.rows) == 1
        assert "Parcels" in dock._results.rows[0].text

    def test_the_count_reports_what_is_shown_not_what_arrived(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"12 results" over a list of 3 rows reads as a broken list."""
        dock = self._dock(mod, monkeypatch)
        dock._render_results([
            _item(id="1", type="data_layer"),
            _item(id="2", type="dashboard"),
            _item(id="3", type="web_app"),
        ])
        assert "1 result" in dock._status.text

    def test_each_row_carries_the_item_for_the_double_click(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row's payload is the only thing the open path gets."""
        dock = self._dock(mod, monkeypatch)
        dock._render_results([_item(id="item-9")])
        assert dock._results.rows[0].payload["id"] == "item-9"


class TestAddTarget:
    """Routing a resolved leaf to the right iface entry point.

    Getting this wrong adds nothing to the canvas and reports no error,
    which is how the old hand-rolled version refused basemaps outright
    while the Browser tree added them happily.
    """

    def _dock(self, mod: ModuleType) -> tuple[Any, list[tuple[str, Any]]]:
        calls: list[tuple[str, Any]] = []
        iface = SimpleNamespace(
            addVectorTileLayer=lambda *a: calls.append(("vector-tile", a)),
            addRasterLayer=lambda *a: calls.append(("raster", a)),
            addVectorLayer=lambda *a: calls.append(("vector", a)),
        )
        dock = mod.GratisGISSearchDock.__new__(mod.GratisGISSearchDock)
        dock._iface = iface
        return dock, calls

    def test_a_vector_tile_layer_goes_to_the_vector_tile_call(
        self, mod: ModuleType
    ) -> None:
        from gratisgis_qgis.layer_targets import LayerTarget

        dock, calls = self._dock(mod)
        dock._add_target(
            LayerTarget(
                uri="type=xyz&url=x", name="Roads",
                provider="", layer_type="vector-tile",
            )
        )
        assert calls[0][0] == "vector-tile"

    def test_a_raster_goes_to_the_raster_call_with_its_provider(
        self, mod: ModuleType
    ) -> None:
        """The provider argument is not optional for a raster.

        addRasterLayer with the wrong provider yields an invalid layer,
        not an exception.
        """
        from gratisgis_qgis.layer_targets import LayerTarget

        dock, calls = self._dock(mod)
        dock._add_target(
            LayerTarget(
                uri="/vsicurl/x", name="Imagery",
                provider="gdal", layer_type="raster",
            )
        )
        assert calls[0] == ("raster", ("/vsicurl/x", "Imagery", "gdal"))

    def test_anything_else_goes_to_the_vector_call(
        self, mod: ModuleType
    ) -> None:
        from gratisgis_qgis.layer_targets import LayerTarget

        dock, calls = self._dock(mod)
        dock._add_target(
            LayerTarget(
                uri="url='x' typename='y'", name="Parcels",
                provider="OAPIF", layer_type="vector",
            )
        )
        assert calls[0] == ("vector", ("url='x' typename='y'", "Parcels", "OAPIF"))


class TestStaleSearchResults:
    """Two searches in flight; the older must not land on the newer.

    Typing a query, pressing Search, then changing it and searching
    again is ordinary use, and the portal does not answer in the order
    it was asked. Without the sequence guard a slow first search
    overwrites the second one's results with stale rows and no
    indication anything happened.
    """

    def _dock(self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Any:
        scheduled: list[tuple[Any, Any, Any]] = []
        monkeypatch.setattr(
            mod,
            "run_in_task",
            lambda _n, fn, done, failed, **kw: scheduled.append(
                (fn, done, failed)
            ),
        )
        monkeypatch.setattr(mod, "list_items", lambda *a, **k: [])

        dock = mod.GratisGISSearchDock.__new__(mod.GratisGISSearchDock)
        dock._search_seq = 0
        dock._store = SimpleNamespace(
            get=lambda _n: SimpleNamespace(is_discovered=True)
        )
        dock._connection_combo = SimpleNamespace(currentData=lambda: "demo")
        dock._type_combo = SimpleNamespace(currentIndex=lambda: 0)
        dock._query_input = SimpleNamespace(text=lambda: "parcels")
        dock._search_btn = SimpleNamespace(setEnabled=lambda _v: None)
        dock._status = SimpleNamespace(
            text="", setText=lambda t: setattr(dock._status, "text", t)
        )
        dock._results = _FakeList()
        rendered: list[list[Any]] = []
        dock.scheduled = scheduled
        dock.rendered = rendered
        dock._render_results = rendered.append  # type: ignore[method-assign]
        return dock

    def test_a_late_first_search_is_discarded(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dock = self._dock(mod, monkeypatch)
        dock._on_search()
        dock._on_search()
        first_done = dock.scheduled[0][1]
        second_done = dock.scheduled[1][1]

        first_done([_item(id="stale")])
        assert dock.rendered == [], "the older search must not render"

        second_done([_item(id="fresh")])
        assert len(dock.rendered) == 1
        assert dock.rendered[0][0].id == "fresh"

    def test_a_late_failure_does_not_overwrite_a_newer_status(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An error box from a search the user has already replaced."""
        dock = self._dock(mod, monkeypatch)
        dock._on_search()
        dock._on_search()
        dock._status.setText("Searching...")

        dock.scheduled[0][2](RuntimeError("portal down"))
        assert "failed" not in dock._status.text.lower()

    def test_the_current_search_still_reports_its_failure(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not swallow the error the user is waiting on."""
        dock = self._dock(mod, monkeypatch)
        dock._on_search()
        dock.scheduled[0][2](RuntimeError("portal down"))
        assert "failed" in dock._status.text.lower()
        assert "portal down" in dock._status.text


class TestSearchPreconditions:
    def _dock(self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch, profile: Any) -> Any:
        scheduled: list[Any] = []
        monkeypatch.setattr(
            mod, "run_in_task", lambda *a, **k: scheduled.append(a)
        )
        dock = mod.GratisGISSearchDock.__new__(mod.GratisGISSearchDock)
        dock._search_seq = 0
        dock._store = SimpleNamespace(get=lambda _n: profile)
        dock._connection_combo = SimpleNamespace(
            currentData=lambda: "demo" if profile is not None else None
        )
        dock._type_combo = SimpleNamespace(currentIndex=lambda: 0)
        dock._query_input = SimpleNamespace(text=lambda: "x")
        dock._search_btn = SimpleNamespace(setEnabled=lambda _v: None)
        dock._status = SimpleNamespace(
            text="", setText=lambda t: setattr(dock._status, "text", t)
        )
        dock._results = _FakeList()
        dock.scheduled = scheduled
        return dock

    def test_no_connection_picked_asks_for_one(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dock = self._dock(mod, monkeypatch, None)
        dock._on_search()
        assert dock.scheduled == [], "no network call without a connection"
        assert "connection" in dock._status.text.lower()

    def test_a_connection_that_is_not_signed_in_says_so(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plain English, and it names where to fix it.

        Letting this through would surface as a raw auth error from
        whichever call happened to run first.
        """
        dock = self._dock(
            mod, monkeypatch, SimpleNamespace(is_discovered=False)
        )
        dock._on_search()
        assert dock.scheduled == []
        assert "sign" in dock._status.text.lower()


class TestMapDoubleClick:
    """A map in the results opens the whole stack, not a single layer.

    Routed before leaf resolution, which only knows how to add one
    layer and would otherwise show the map's tooltip in a message box.
    """

    def test_a_map_row_launches_the_open_flow(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launched: list[tuple[str, str]] = []
        import gratisgis_qgis.open_map as open_map_mod

        monkeypatch.setattr(
            open_map_mod,
            "launch_open_map",
            lambda _p, item_id, title, _i: launched.append((item_id, title)),
        )
        resolved: list[Any] = []
        monkeypatch.setattr(
            mod, "run_in_task", lambda *a, **k: resolved.append(a)
        )
        profile = SimpleNamespace(name="demo")
        dock = mod.GratisGISSearchDock.__new__(mod.GratisGISSearchDock)
        dock._store = SimpleNamespace(get=lambda _n: profile)
        dock._connection_combo = SimpleNamespace(currentData=lambda: "demo")
        dock._iface = SimpleNamespace()

        payload = _item(id="map-7", type="map", title="WV overview").to_api_dict()
        row = SimpleNamespace(data=lambda _role: payload)
        dock._on_double_click(row)
        assert launched == [("map-7", "WV overview")]
        assert not resolved, "no leaf resolution task for a map"
