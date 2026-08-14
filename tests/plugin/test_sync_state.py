# SPDX-License-Identifier: AGPL-3.0-or-later
"""Working out what an offline clone owes the portal.

These cover the rules the sync turns on. The first push flow read
QGIS's unsaved edit buffer, which meant edits vanished the moment you
saved, nothing recorded what had already been sent, and pushing and
then discarding left the portal holding changes the local file never
had. State now lives in the clone, so these tests are about comparing
a file against the baseline it was cloned with.
"""
from __future__ import annotations

import pytest

from gratisgis_qgis.offline.sync_state import (
    BaselineEntry,
    LocalFeature,
    find_conflicts,
    hash_attributes,
    hash_geometry,
    new_global_id,
    plan_local_changes,
    summarize_changes,
)


def _local(
    global_id: str | None,
    *,
    attr: str = "a",
    geom: str = "g",
    fid: int | None = 1,
    properties: dict | None = None,
) -> LocalFeature:
    return LocalFeature(
        qgis_fid=fid,
        global_id=global_id,
        attr_hash=attr,
        geom_hash=geom,
        geometry={"type": "Point", "coordinates": [0, 0]},
        properties=properties if properties is not None else {"name": "x"},
    )


def _base(attr: str = "a", geom: str = "g", edited: str | None = "T1") -> BaselineEntry:
    return BaselineEntry(attr_hash=attr, geom_hash=geom, portal_edited_at=edited)


class TestHashing:
    def test_key_order_does_not_change_the_hash(self) -> None:
        assert hash_attributes({"a": 1, "b": 2}) == hash_attributes({"b": 2, "a": 1})

    def test_a_changed_value_changes_the_hash(self) -> None:
        assert hash_attributes({"a": 1}) != hash_attributes({"a": 2})

    @pytest.mark.parametrize("key", ["fid", "_portal_id", "_edited_at", "_created_by"])
    def test_bookkeeping_columns_are_not_user_data(self, key: str) -> None:
        # The portal restamps _edited_at on every server-side write, and
        # fid is the GeoPackage's own row id. Counting either as user
        # data would report an edit on a feature nobody touched.
        assert hash_attributes({"name": "x"}) == hash_attributes({"name": "x", key: "z"})

    def test_missing_geometry_hashes_as_empty(self) -> None:
        assert hash_geometry(None) == ""
        assert hash_geometry(b"") == ""

    def test_different_geometries_hash_differently(self) -> None:
        assert hash_geometry(b"\x01\x02") != hash_geometry(b"\x01\x03")

    def test_minted_ids_are_unique(self) -> None:
        assert new_global_id() != new_global_id()


class TestPlanLocalChanges:
    def test_an_untouched_feature_produces_nothing(self) -> None:
        assert plan_local_changes([_local("f1")], {"f1": _base()}) == []

    def test_a_changed_attribute_is_an_update(self) -> None:
        [change] = plan_local_changes([_local("f1", attr="CHANGED")], {"f1": _base()})
        assert (change.kind, change.portal_id) == ("update", "f1")

    def test_a_moved_geometry_is_an_update(self) -> None:
        [change] = plan_local_changes([_local("f1", geom="MOVED")], {"f1": _base()})
        assert change.kind == "update"

    def test_a_row_with_no_id_is_a_create_and_gets_one(self) -> None:
        [change] = plan_local_changes([_local(None)], {})
        assert change.kind == "create"
        assert change.portal_id

    def test_a_row_absent_from_the_baseline_is_resent_under_its_own_id(self) -> None:
        # The state a create leaves behind when its response was lost:
        # the id is already in the file but the baseline never got it.
        # Resending is right, and safe, because the portal dedupes an
        # append on the id.
        [change] = plan_local_changes([_local("minted-1")], {})
        assert (change.kind, change.portal_id) == ("create", "minted-1")

    def test_a_missing_row_is_a_delete(self) -> None:
        [change] = plan_local_changes([], {"f1": _base()})
        assert (change.kind, change.portal_id) == ("delete", "f1")

    def test_deletes_are_found_without_anything_having_watched(self) -> None:
        # The point of a baseline over a change log: a feature deleted
        # and saved in a previous QGIS session is still detected.
        changes = plan_local_changes([_local("f1")], {"f1": _base(), "f2": _base()})
        assert [(c.kind, c.portal_id) for c in changes] == [("delete", "f2")]

    def test_internal_columns_are_not_sent_as_attributes(self) -> None:
        [change] = plan_local_changes(
            [_local("f1", attr="CHANGED", properties={"name": "x", "_portal_id": "f1"})],
            {"f1": _base()},
        )
        assert change.properties == {"name": "x"}

    def test_a_mixed_bag_is_all_accounted_for(self) -> None:
        changes = plan_local_changes(
            [_local("keep"), _local("edit", attr="X"), _local(None, fid=9)],
            {"keep": _base(), "edit": _base(), "gone": _base()},
        )
        assert sorted(c.kind for c in changes) == ["create", "delete", "update"]


class TestConflicts:
    def _changes(self, kind: str, portal_id: str):
        from gratisgis_qgis.edit.sync import EditedFeature

        return [EditedFeature(kind=kind, portal_id=portal_id, qgis_fid=1)]

    def test_no_conflict_when_the_portal_has_not_moved(self) -> None:
        found = find_conflicts(
            self._changes("update", "f1"), {"f1": _base(edited="T1")}, {"f1": "T1"}
        )
        assert found == []

    def test_conflict_when_both_sides_changed(self) -> None:
        [conflict] = find_conflicts(
            self._changes("update", "f1"), {"f1": _base(edited="T1")}, {"f1": "T2"}
        )
        assert conflict.global_id == "f1"
        assert "portal" in conflict.detail.lower()

    def test_conflict_when_the_portal_deleted_what_we_updated(self) -> None:
        [conflict] = find_conflicts(
            self._changes("update", "f1"), {"f1": _base()}, {}
        )
        assert "deleted on the portal" in conflict.detail.lower()

    def test_both_deleting_is_agreement_not_conflict(self) -> None:
        assert find_conflicts(self._changes("delete", "f1"), {"f1": _base()}, {}) == []

    def test_a_create_is_never_a_conflict(self) -> None:
        # The portal has not seen it, so it cannot have changed there.
        assert find_conflicts(self._changes("create", "new-1"), {}, {}) == []

    def test_an_unknown_stamp_does_not_invent_a_conflict(self) -> None:
        # Better to send than to cry conflict on missing information.
        assert (
            find_conflicts(
                self._changes("update", "f1"), {"f1": _base(edited=None)}, {"f1": "T2"}
            )
            == []
        )


class TestSummary:
    def test_says_so_when_there_is_nothing(self) -> None:
        assert summarize_changes([]) == "No changes to send."

    def test_counts_in_plain_words(self) -> None:
        changes = plan_local_changes(
            [_local("edit", attr="X"), _local(None, fid=2)],
            {"edit": _base(), "gone": _base()},
        )
        text = summarize_changes(changes)
        assert "1 added" in text and "1 changed" in text and "1 removed" in text
