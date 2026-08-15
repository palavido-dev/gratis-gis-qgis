# SPDX-License-Identifier: AGPL-3.0-or-later
"""What happens when someone else changed the same features.

The sync path is the only place in the plugin where a user's action can
destroy work that is not theirs. The portal accepts no version token on
a write, so there is no "only if it is still what I read" and no honest
merge: the dialog names the collisions and asks the user to pick a side.

Both halves of that decision were untested. The summary is what the
user reads before choosing, and the filter is what actually gets sent
after they choose. Getting either wrong is silent in both directions:
drop too much and the rest of their edits never arrive, drop too little
and the overwrite they declined happens anyway.

The message box itself is not tested. Which button was clicked is Qt's
business; what each answer means is this file's.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub

_WIDGETS = [
    "QApplication", "QCheckBox", "QComboBox", "QDialog", "QDialogButtonBox",
    "QFormLayout", "QHBoxLayout", "QLabel", "QLineEdit", "QListWidget",
    "QListWidgetItem", "QMessageBox", "QPlainTextEdit", "QProgressBar",
    "QPushButton", "QTextEdit", "QVBoxLayout", "QWidget",
]


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                name: type(name, (), {})
                for name in (
                    "QgsFeature", "QgsGeometry", "QgsProject",
                    "QgsVectorLayer", "QgsJsonUtils", "QgsWkbTypes",
                )
            },
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QSettings": type("QSettings", (), {}),
                "QTimer": type("QTimer", (), {}),
                "QVariant": type("QVariant", (), {}),
            },
            "qgis.PyQt.QtWidgets": {n: type(n, (), {}) for n in _WIDGETS},
        },
    )
    import gratisgis_qgis.ui.push_edits_dialog as m

    return m


def _conflict(global_id: str, detail: str = "") -> Any:
    return SimpleNamespace(
        global_id=global_id, detail=detail or f"feature {global_id}"
    )


def _op(portal_id: str | None, kind: str = "update") -> Any:
    return SimpleNamespace(portal_id=portal_id, kind=kind, qgis_fid=1)


class TestConflictSummary:
    def test_every_conflict_is_named_when_there_are_few(
        self, mod: ModuleType
    ) -> None:
        text = mod.conflict_summary([_conflict("a"), _conflict("b")])
        assert "feature a" in text and "feature b" in text
        assert "more" not in text

    def test_a_long_list_is_truncated_and_says_how_many_are_hidden(
        self, mod: ModuleType
    ) -> None:
        """Silently showing ten of four hundred understates the collision.

        The user is deciding whether to overwrite someone else's work
        from this text. "10 rows" and "400 rows" are different
        decisions.
        """
        conflicts = [_conflict(str(i)) for i in range(400)]
        text = mod.conflict_summary(conflicts)
        assert text.count("\n") == mod.CONFLICT_DETAIL_LIMIT
        assert f"and {400 - mod.CONFLICT_DETAIL_LIMIT} more" in text

    def test_exactly_at_the_limit_says_nothing_about_more(
        self, mod: ModuleType
    ) -> None:
        """An off-by-one here reads as "and 0 more"."""
        conflicts = [_conflict(str(i)) for i in range(mod.CONFLICT_DETAIL_LIMIT)]
        text = mod.conflict_summary(conflicts)
        assert "more" not in text

    def test_one_over_the_limit_reports_one_more(self, mod: ModuleType) -> None:
        conflicts = [
            _conflict(str(i)) for i in range(mod.CONFLICT_DETAIL_LIMIT + 1)
        ]
        assert "and 1 more" in mod.conflict_summary(conflicts)

    def test_no_conflicts_produces_no_text(self, mod: ModuleType) -> None:
        assert mod.conflict_summary([]) == ""


class TestOpsWithoutConflicts:
    def test_conflicting_features_are_dropped(self, mod: ModuleType) -> None:
        ops = [_op("a"), _op("b"), _op("c")]
        kept = mod.ops_without_conflicts(ops, [_conflict("b")])
        assert [o.portal_id for o in kept] == ["a", "c"]

    def test_the_rest_are_kept(self, mod: ModuleType) -> None:
        """"Skip those, send the rest" has to actually send the rest.

        Dropping everything on any conflict would look like a
        successful sync that quietly did nothing.
        """
        ops = [_op("a"), _op("b")]
        assert len(mod.ops_without_conflicts(ops, [_conflict("a")])) == 1

    def test_nothing_is_dropped_when_nothing_conflicts(
        self, mod: ModuleType
    ) -> None:
        ops = [_op("a"), _op("b")]
        assert mod.ops_without_conflicts(ops, []) == ops

    def test_newly_created_features_are_never_conflicts(
        self, mod: ModuleType
    ) -> None:
        """A create has no portal id, so nobody else can have touched it.

        Matching on ``portal_id`` means every create carries None. If a
        conflict ever arrived with a None id, a naive membership test
        would drop every new feature the user drew.
        """
        ops = [_op(None, kind="create"), _op(None, kind="create"), _op("b")]
        kept = mod.ops_without_conflicts(ops, [_conflict("b")])
        assert len(kept) == 2
        assert all(o.kind == "create" for o in kept)

    def test_order_is_preserved(self, mod: ModuleType) -> None:
        """Deletes before creates matters when ids are reused."""
        ops = [_op("a"), _op("b"), _op("c"), _op("d")]
        kept = mod.ops_without_conflicts(ops, [_conflict("c")])
        assert [o.portal_id for o in kept] == ["a", "b", "d"]

    def test_a_conflict_naming_something_not_being_sent_is_harmless(
        self, mod: ModuleType
    ) -> None:
        """The portal reports changes across the layer, not just ours."""
        ops = [_op("a")]
        assert mod.ops_without_conflicts(ops, [_conflict("zzz")]) == ops


class TestUnsavedEdits:
    """Only saved work is sent, and why that is not fussiness.

    The buffer version could push edits still sitting unsaved in QGIS.
    Answering "discard" afterwards left the portal holding changes the
    local file never had, with nothing aware the two had diverged.
    """

    def test_a_modified_layer_is_refused_with_instructions(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mod, "_gpkg_path_from_source", lambda _s: "C:/c.gpkg"
        )
        monkeypatch.setattr(
            mod, "read_clone_source",
            lambda _p: SimpleNamespace(item_id="i", layer_id="default"),
        )
        monkeypatch.setattr(mod, "has_baseline", lambda _p: True)
        layer = SimpleNamespace(
            source=lambda: "C:/c.gpkg", isModified=lambda: True
        )

        changes, reason = mod._collect_changes(layer)
        assert changes == []
        assert "save" in reason.lower()

    def test_a_clone_from_an_older_plugin_says_to_clone_again(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No baseline means no way to tell what changed.

        Sending everything would be the alternative, and that pushes
        unchanged rows over whatever the portal has now.
        """
        monkeypatch.setattr(
            mod, "_gpkg_path_from_source", lambda _s: "C:/c.gpkg"
        )
        monkeypatch.setattr(
            mod, "read_clone_source",
            lambda _p: SimpleNamespace(item_id="i", layer_id="default"),
        )
        monkeypatch.setattr(mod, "has_baseline", lambda _p: False)
        layer = SimpleNamespace(
            source=lambda: "C:/c.gpkg", isModified=lambda: False
        )

        changes, reason = mod._collect_changes(layer)
        assert changes == []
        assert "clone" in reason.lower()

    def test_a_layer_with_no_isModified_is_not_assumed_dirty(
        self, mod: ModuleType
    ) -> None:
        """Some layer classes do not have it; that is not "unsaved".

        Assuming dirty would refuse to sync layers that are perfectly
        fine, with a message telling the user to save edits they do not
        have.
        """
        assert mod._layer_has_unsaved_edits(SimpleNamespace()) is False

    def test_a_live_layer_still_uses_the_edit_buffer(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not every syncable layer is a clone.

        A live OAPIF layer has no local file to baseline against, so
        the buffer is the only record of the change that exists.
        """
        monkeypatch.setattr(mod, "_gpkg_path_from_source", lambda _s: None)
        sentinel = [object()]
        monkeypatch.setattr(mod, "_collect_edits", lambda _l: sentinel)
        layer = SimpleNamespace(source=lambda: "url='x' typename='y'")

        changes, reason = mod._collect_changes(layer)
        assert changes is sentinel
        assert reason == ""
