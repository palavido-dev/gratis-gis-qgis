# SPDX-License-Identifier: AGPL-3.0-or-later
"""Carrying styling and tree position across a clone overwrite (#17).

Whether QGIS's named-style XML actually round-trips is a question about
real serialisation, and it is asserted in ``scripts/qgis_smoke.py``
where the answer means something. What is pinned here is the part a
stub can answer honestly: that nothing in this module can fail the
overwrite it is decorating.

That constraint is the whole design. The file has already been written
by the time any of this runs, so raising would leave the project
pointing at data that no longer matches what it says. Losing symbology
is a nuisance; that is not.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from gratisgis_qgis.layer_placement import (
    LayerPlacement,
    capture_placement,
    restore_placement,
)
from tests.plugin.conftest import install_qgis_stub


class _Exploding:
    """A layer that raises from every accessor this module uses."""

    def id(self) -> str:
        raise RuntimeError("layer is gone")

    def exportNamedStyle(self, *_args: Any) -> None:  # QGIS API name
        raise RuntimeError("style unreadable")

    def importNamedStyle(self, *_args: Any) -> None:  # QGIS API name
        raise RuntimeError("style unwritable")

    def triggerRepaint(self) -> None:  # QGIS API name
        raise RuntimeError("no")


class TestNothingCanFailTheOverwrite:
    def test_capturing_from_a_hostile_layer_returns_an_empty_placement(
        self,
    ) -> None:
        placement = capture_placement(_Exploding())
        assert placement.is_empty

    def test_capturing_without_qgis_at_all_is_survivable(self) -> None:
        """No qgis in sys.modules is what the test runner actually is.

        Reaching the import inside a try is the guard; if it ever moved
        to module level this test would stop the whole file importing.
        """
        assert capture_placement(SimpleNamespace()).is_empty

    def test_restoring_a_hostile_layer_does_not_raise(self) -> None:
        placement = LayerPlacement(
            style_xml="<qgis/>", group_path=["Reference"], index=2
        )
        restore_placement(_Exploding(), placement)

    def test_restoring_an_empty_placement_does_nothing_at_all(self) -> None:
        """Nothing was captured, so there is nothing to put back.

        Worth its own test because the alternative is applying an empty
        style, which QGIS reads as "reset this layer to defaults" and
        would make the no-op case actively destructive.
        """
        touched: list[str] = []

        def note(what: str, result: Any = None) -> Any:
            touched.append(what)
            return result

        layer = SimpleNamespace(
            id=lambda: note("id", "x"),
            importNamedStyle=lambda _d: note("style"),
        )
        restore_placement(layer, LayerPlacement())
        assert touched == []


class TestIsEmpty:
    def test_a_fresh_placement_is_empty(self) -> None:
        assert LayerPlacement().is_empty

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"style_xml": "<qgis/>"},
            {"group_path": ["Reference"]},
            {"index": 0},
        ],
        ids=["style", "group", "position"],
    )
    def test_any_captured_detail_makes_it_worth_restoring(
        self, kwargs: dict[str, Any]
    ) -> None:
        """Each of the three travels independently.

        A layer at the top level in position 3 has no group path but a
        real position, and treating that as nothing to do would drop it
        to the bottom of the list.
        """
        assert not LayerPlacement(**kwargs).is_empty

    def test_index_zero_counts_as_captured(self) -> None:
        """The top of a group is a position, not an absence.

        ``-1`` is the sentinel; ``0`` is the most common real answer,
        and a falsy check here would silently skip every layer sitting
        at the top of its group.
        """
        assert not LayerPlacement(index=0).is_empty


class TestGroupPath:
    """Reading where a layer sits, against a stubbed layer tree."""

    def _install(
        self, monkeypatch: pytest.MonkeyPatch, node: Any
    ) -> ModuleType:
        root = SimpleNamespace(findLayer=lambda _id: node)
        project = SimpleNamespace(layerTreeRoot=lambda: root)
        install_qgis_stub(
            monkeypatch,
            {
                "qgis.core": {
                    "QgsProject": SimpleNamespace(instance=lambda: project),
                    "QgsLayerTreeLayer": type("QgsLayerTreeLayer", (), {}),
                },
            },
        )
        import gratisgis_qgis.layer_placement as m

        return m

    def _group(self, name: str, parent: Any) -> Any:
        return SimpleNamespace(
            name=lambda n=name: n, parent=lambda p=parent: p, children=list
        )

    def test_a_layer_at_the_top_level_has_no_group_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root is not a group; it must not appear in the path.

        Including it would make every path start with the root's name
        and never match on the way back.
        """
        root = self._group("", None)
        node = SimpleNamespace(parent=lambda: root)
        mod = self._install(monkeypatch, node)
        assert mod._group_path(SimpleNamespace(id=lambda: "x")) == []

    def test_nested_groups_are_reported_root_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order is what makes the path walkable on the way back."""
        root = self._group("", None)
        outer = self._group("Reference", root)
        inner = self._group("Boundaries", outer)
        node = SimpleNamespace(parent=lambda: inner)
        mod = self._install(monkeypatch, node)
        assert mod._group_path(SimpleNamespace(id=lambda: "x")) == [
            "Reference", "Boundaries"
        ]

    def test_a_layer_not_in_the_tree_has_no_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Added with addToLegend=False, or already removed."""
        mod = self._install(monkeypatch, None)
        assert mod._group_path(SimpleNamespace(id=lambda: "x")) == []
        assert mod._index_in_parent(SimpleNamespace(id=lambda: "x")) == -1


class TestFindGroup:
    def test_a_missing_group_is_not_recreated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user may have deleted it while the clone downloaded.

        Conjuring it back would be the plugin overruling that, and the
        layer would reappear in a group they had just removed.
        """
        import gratisgis_qgis.layer_placement as mod

        root = SimpleNamespace(children=lambda: [])
        assert mod._find_group(root, ["Reference"]) is None

    def test_the_root_is_returned_for_an_empty_path(self) -> None:
        import gratisgis_qgis.layer_placement as mod

        root = SimpleNamespace(children=lambda: [])
        assert mod._find_group(root, []) is root

    def test_a_layer_node_is_not_mistaken_for_a_group(self) -> None:
        """Both are tree nodes and both have a name.

        Matching on name alone would walk into a layer called
        "Reference" and then look for children it does not have.
        """
        import gratisgis_qgis.layer_placement as mod

        layer_node = SimpleNamespace(name=lambda: "Reference")
        root = SimpleNamespace(children=lambda: [layer_node])
        assert mod._find_group(root, ["Reference"]) is None
