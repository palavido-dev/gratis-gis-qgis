# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the edit-sync planner.

The planner is pure-Python; it takes a list of EditedFeature
records describing what changed in QGIS and emits an ordered
SyncOp list the dialog can execute against the portal's CRUD
endpoints.

The rules under test are the ones that protect the user from
foot-guns: ordering (creates first, then updates, then deletes),
update merging (so co-occurring geom + attr edits don't pay 2x
round-trips), delete-supersedes-update, and the skip-with-reason
treatment of impossible operations (update without portal_id, etc.).
"""
from __future__ import annotations

from gratisgis_qgis.edit.sync import (
    EditedFeature,
    SkippedEdit,
    SyncOp,
    SyncPlan,
    build_sync_plan,
    summarize_plan,
)


def _make(
    kind: str,
    *,
    qgis_fid: int | None = 1,
    portal_id: str | None = None,
    geometry: dict | None = None,
    properties: dict | None = None,
) -> EditedFeature:
    return EditedFeature(
        kind=kind,  # type: ignore[arg-type]
        portal_id=portal_id,
        qgis_fid=qgis_fid,
        geometry=geometry,
        properties=properties,
    )


class TestPlanOrdering:
    def test_creates_run_before_updates_before_deletes(self) -> None:
        # Survey123 and AGO mobile use this same order because a
        # partial run still leaves the portal in a consistent state:
        # creates land first so the server can assign ids, updates
        # land second, deletes go last (so a transient mistake can
        # be recovered before the deletes commit).
        edits = [
            _make("delete", portal_id="d1"),
            _make("update", portal_id="u1", properties={"a": 1}),
            _make("create", properties={"a": 1}),
        ]
        plan = build_sync_plan(edits)
        kinds = [op.kind for op in plan.ops]
        assert kinds == ["create", "update", "delete"]

    def test_within_a_kind_input_order_is_preserved(self) -> None:
        # The dialog's progress UX feels jumpy when ops get re-
        # shuffled run to run. Preserving input order makes "op
        # 3 of 7 failed" reproducible.
        edits = [
            _make("create", qgis_fid=10, properties={"a": 1}),
            _make("create", qgis_fid=20, properties={"a": 2}),
            _make("create", qgis_fid=30, properties={"a": 3}),
        ]
        plan = build_sync_plan(edits)
        assert [op.qgis_fid for op in plan.ops] == [10, 20, 30]


class TestUpdateMerging:
    def test_geometry_and_attribute_changes_to_same_id_merge(self) -> None:
        # The single most important optimization: QGIS reports
        # geom and attr edits as separate buffer entries, but the
        # portal accepts both in one PATCH. Merging halves the
        # round-trip count for typical workflows.
        edits = [
            _make("update", portal_id="x", geometry={"type": "Point", "coordinates": [1, 2]}),
            _make("update", portal_id="x", properties={"name": "Foo"}),
        ]
        plan = build_sync_plan(edits)
        assert len(plan.ops) == 1
        op = plan.ops[0]
        assert op.kind == "update"
        assert op.portal_id == "x"
        assert op.geometry == {"type": "Point", "coordinates": [1, 2]}
        assert op.properties == {"name": "Foo"}

    def test_attribute_merges_take_latest_value(self) -> None:
        # QGIS edit-buffer entries are in the order the user made
        # them. The latest value wins so "set X to A, then change
        # mind and set X to B" pushes B.
        edits = [
            _make("update", portal_id="x", properties={"k": "first"}),
            _make("update", portal_id="x", properties={"k": "second"}),
        ]
        plan = build_sync_plan(edits)
        assert plan.ops[0].properties == {"k": "second"}

    def test_geometry_edits_to_same_id_keep_the_latest(self) -> None:
        edits = [
            _make("update", portal_id="x", geometry={"type": "Point", "coordinates": [1, 1]}),
            _make("update", portal_id="x", geometry={"type": "Point", "coordinates": [2, 2]}),
        ]
        plan = build_sync_plan(edits)
        assert len(plan.ops) == 1
        assert plan.ops[0].geometry == {"type": "Point", "coordinates": [2, 2]}

    def test_no_op_update_is_dropped_silently(self) -> None:
        # The QGIS buffer occasionally emits update entries with
        # neither geometry nor properties (transient selection
        # toggles). Don't surface these as skipped because they're
        # not user-actionable; drop them.
        edits = [_make("update", portal_id="x")]
        plan = build_sync_plan(edits)
        assert plan.ops == []
        assert plan.skipped == []


class TestDeleteSupersedesUpdate:
    def test_delete_after_update_drops_the_update(self) -> None:
        # If the user edits a feature and then deletes it before
        # pushing, the update would 404 against the deleted row.
        # Drop the update; only the delete survives.
        edits = [
            _make("update", portal_id="x", properties={"k": "v"}),
            _make("delete", portal_id="x"),
        ]
        plan = build_sync_plan(edits)
        assert [op.kind for op in plan.ops] == ["delete"]
        assert plan.ops[0].portal_id == "x"

    def test_delete_after_unrelated_update_keeps_both(self) -> None:
        edits = [
            _make("update", portal_id="x", properties={"k": "v"}),
            _make("delete", portal_id="y"),
        ]
        plan = build_sync_plan(edits)
        assert [op.kind for op in plan.ops] == ["update", "delete"]


class TestSkipReasons:
    def test_update_without_portal_id_is_skipped(self) -> None:
        # The local feature was created in QGIS but never pushed,
        # so the portal has no row to PATCH. The user fix is to
        # push the create first.
        edits = [_make("update", portal_id=None, properties={"k": "v"})]
        plan = build_sync_plan(edits)
        assert plan.ops == []
        assert len(plan.skipped) == 1
        assert "portal id" in plan.skipped[0].reason

    def test_delete_without_portal_id_is_skipped(self) -> None:
        edits = [_make("delete", portal_id=None)]
        plan = build_sync_plan(edits)
        assert plan.ops == []
        assert len(plan.skipped) == 1

    def test_empty_create_is_skipped(self) -> None:
        # A create with no geometry AND no properties is a no-op
        # against the portal; skip with reason so the user knows
        # the row didn't drop into the void silently.
        edits = [_make("create", geometry=None, properties=None)]
        plan = build_sync_plan(edits)
        assert plan.ops == []
        assert len(plan.skipped) == 1
        assert "empty" in plan.skipped[0].reason.lower()


class TestEmptyAndBoundary:
    def test_empty_edit_list_produces_empty_plan(self) -> None:
        plan = build_sync_plan([])
        assert plan == SyncPlan(ops=[], skipped=[])

    def test_create_with_only_properties_is_kept(self) -> None:
        # Table layers (no geom) routinely produce property-only
        # creates; those are valid inserts.
        plan = build_sync_plan([_make("create", properties={"name": "X"})])
        assert len(plan.ops) == 1
        assert plan.ops[0].geometry is None
        assert plan.ops[0].properties == {"name": "X"}

    def test_create_with_only_geometry_is_kept(self) -> None:
        # Geometry-only creates are unusual but valid (a sketch
        # without attributes); accept them.
        plan = build_sync_plan(
            [_make("create", geometry={"type": "Point", "coordinates": [0, 0]})]
        )
        assert len(plan.ops) == 1
        assert plan.ops[0].geometry == {"type": "Point", "coordinates": [0, 0]}
        assert plan.ops[0].properties is None


class TestSummarize:
    def test_summarize_counts_each_kind(self) -> None:
        plan = SyncPlan(
            ops=[
                SyncOp(kind="create", qgis_fid=1, portal_id=None),
                SyncOp(kind="create", qgis_fid=2, portal_id=None),
                SyncOp(kind="update", qgis_fid=3, portal_id="u"),
                SyncOp(kind="delete", qgis_fid=4, portal_id="d"),
            ],
            skipped=[SkippedEdit(qgis_fid=5, reason="x")],
        )
        line = summarize_plan(plan)
        assert "2 create" in line
        assert "1 update" in line
        assert "1 delete" in line
        assert "1 skipped" in line

    def test_summarize_zero_state(self) -> None:
        line = summarize_plan(SyncPlan())
        assert "0 create" in line
        assert "0 skipped" in line
