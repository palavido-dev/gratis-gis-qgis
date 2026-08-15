# SPDX-License-Identifier: AGPL-3.0-or-later
"""Splitting a feature in a clone, and syncing both halves.

Reported from a real session: a polygon split in the offline layer,
synced, and only one half appeared on the portal. Syncing again still
offered the same single edit, forever.

One cause. QGIS's Split Features keeps the original feature and adds
another for the new part, copying every attribute across, portal id
included. Two local rows then claim the same portal feature, and two
separate places collapse them without saying so:

- ``build_sync_plan`` merges updates by portal id, which is right for
  the case it was written for (a geometry edit and an attribute edit to
  one feature) and wrong here. Only one half was ever sent.
- ``baseline_from_features`` is a dict keyed by portal id, so the two
  rows leave one entry. Whichever row it did not describe read as
  edited on every subsequent sync.

Neither failure raises. The sync reports success both times.
"""
from __future__ import annotations

from typing import Any

from gratisgis_qgis.edit.sync import build_sync_plan
from gratisgis_qgis.offline.reader import baseline_from_features
from gratisgis_qgis.offline.sync_state import (
    BaselineEntry,
    LocalFeature,
    plan_local_changes,
)

_ID = "01a00350-f0c5-71b6-9aea-1b5088dc676a"


def _feature(
    fid: int,
    global_id: str | None,
    *,
    attr_hash: str = "attrs",
    geom_hash: str = "geom",
    geometry: dict[str, Any] | None = None,
) -> LocalFeature:
    return LocalFeature(
        qgis_fid=fid,
        global_id=global_id,
        attr_hash=attr_hash,
        geom_hash=geom_hash,
        geometry=geometry or {"type": "Polygon", "coordinates": []},
        properties={"_portal_id": global_id, "owner": "matt"},
    )


def _baseline(**overrides: Any) -> dict[str, BaselineEntry]:
    entry = BaselineEntry(
        attr_hash="attrs", geom_hash="geom-before", portal_edited_at=None
    )
    return {_ID: overrides.get("entry", entry)}


class TestSplitProducesTwoChanges:
    """The whole bug, stated once.

    Two rows carrying one id must become one update and one create, not
    one update and silence.
    """

    def test_both_halves_are_sent(self) -> None:
        changes = plan_local_changes(
            [
                _feature(1, _ID, geom_hash="geom-top"),
                _feature(2, _ID, geom_hash="geom-bottom"),
            ],
            _baseline(),
        )
        kinds = sorted(c.kind for c in changes)
        assert kinds == ["create", "update"], (
            f"the split's second half went missing: {kinds}"
        )

    def test_the_new_half_gets_an_id_of_its_own(self) -> None:
        """Reusing the copied id would send it back into the same merge.

        The plan builder keys updates by portal id, so a create sharing
        that id lands the two ops on one feature again and the bug
        survives the fix.
        """
        changes = plan_local_changes(
            [
                _feature(1, _ID, geom_hash="a"),
                _feature(2, _ID, geom_hash="b"),
            ],
            _baseline(),
        )
        created = next(c for c in changes if c.kind == "create")
        assert created.portal_id
        assert created.portal_id != _ID

    def test_the_original_feature_keeps_the_portal_id(self) -> None:
        """Features arrive in fid order, so the first is the original.

        Either half could keep it and the portal ends up with the same
        two geometries, but this way the feature that kept its identity
        locally keeps it on the portal too.
        """
        changes = plan_local_changes(
            [
                _feature(1, _ID, geom_hash="a"),
                _feature(2, _ID, geom_hash="b"),
            ],
            _baseline(),
        )
        update = next(c for c in changes if c.kind == "update")
        assert update.portal_id == _ID
        assert update.qgis_fid == 1

    def test_the_new_half_carries_its_own_geometry(self) -> None:
        """Not the original's; that is the half that disappeared."""
        changes = plan_local_changes(
            [
                _feature(1, _ID, geom_hash="a", geometry={"type": "P", "id": "top"}),
                _feature(2, _ID, geom_hash="b", geometry={"type": "P", "id": "bottom"}),
            ],
            _baseline(),
        )
        created = next(c for c in changes if c.kind == "create")
        assert created.geometry == {"type": "P", "id": "bottom"}

    def test_the_copied_id_is_not_sent_as_an_attribute(self) -> None:
        """It is our bookkeeping column, and it is now the wrong value."""
        changes = plan_local_changes(
            [_feature(1, _ID), _feature(2, _ID, geom_hash="b")],
            _baseline(),
        )
        created = next(c for c in changes if c.kind == "create")
        assert created.properties == {"owner": "matt"}

    def test_three_way_split_sends_three_features(self) -> None:
        """Splitting twice before syncing is ordinary use."""
        changes = plan_local_changes(
            [
                _feature(1, _ID, geom_hash="a"),
                _feature(2, _ID, geom_hash="b"),
                _feature(3, _ID, geom_hash="c"),
            ],
            _baseline(),
        )
        assert sorted(c.kind for c in changes) == [
            "create", "create", "update"
        ]
        ids = {c.portal_id for c in changes}
        assert len(ids) == 3, f"the new halves share an id: {ids}"


class TestThePlanNoLongerCollapses:
    """Through the plan builder, which is where the loss happened."""

    def test_two_ops_survive_into_the_plan(self) -> None:
        plan = build_sync_plan(
            plan_local_changes(
                [
                    _feature(1, _ID, geom_hash="a"),
                    _feature(2, _ID, geom_hash="b"),
                ],
                _baseline(),
            )
        )
        assert len(plan.ops) == 2, (
            f"the plan merged them again: {[op.kind for op in plan.ops]}"
        )
        assert plan.skipped == []

    def test_merging_by_portal_id_still_works_where_it_should(self) -> None:
        """The merge is right for what it was written for.

        A geometry edit and an attribute edit to ONE feature are two
        records for one feature and belong in one PATCH. That is a
        different situation from two rows, and it must not be broken by
        fixing the other.
        """
        from gratisgis_qgis.edit.sync import EditedFeature

        plan = build_sync_plan([
            EditedFeature(
                kind="update", portal_id=_ID, qgis_fid=1,
                geometry={"type": "P"}, properties={},
            ),
            EditedFeature(
                kind="update", portal_id=_ID, qgis_fid=1,
                geometry=None, properties={"owner": "matt"},
            ),
        ])
        assert len(plan.ops) == 1
        assert plan.ops[0].geometry is not None
        assert plan.ops[0].properties == {"owner": "matt"}


class TestTheBaselineStopsRepeating:
    """The second symptom: one edit still pending after a clean sync."""

    def test_two_rows_with_one_id_leave_one_baseline_entry(self) -> None:
        """The mechanism, pinned so the fix is understood as necessary.

        The baseline is a dict keyed by portal id. This is not a bug in
        the baseline; it is why the ids have to be made distinct before
        it is written.
        """
        baseline = baseline_from_features([
            _feature(1, _ID, geom_hash="a"),
            _feature(2, _ID, geom_hash="b"),
        ])
        assert len(baseline) == 1

    def test_distinct_ids_leave_a_full_baseline(self) -> None:
        baseline = baseline_from_features([
            _feature(1, _ID, geom_hash="a"),
            _feature(2, "other-id", geom_hash="b"),
        ])
        assert len(baseline) == 2

    def test_a_synced_split_reports_nothing_pending_next_time(self) -> None:
        """End to end, and the symptom Matt actually saw.

        Split, sync, re-record the baseline from the file as it now is
        (both rows carrying their own id, which is what the minted-id
        write-back leaves behind), and ask again. Before the fix this
        answered "1 update" every time, forever.
        """
        changes = plan_local_changes(
            [
                _feature(1, _ID, geom_hash="top"),
                _feature(2, _ID, geom_hash="bottom"),
            ],
            _baseline(),
        )
        minted = next(c.portal_id for c in changes if c.kind == "create")

        after_sync = [
            _feature(1, _ID, geom_hash="top"),
            _feature(2, minted, geom_hash="bottom"),
        ]
        baseline = baseline_from_features(after_sync)
        assert len(baseline) == 2

        assert plan_local_changes(after_sync, baseline) == []


class TestNothingElseChanged:
    def test_an_untouched_row_is_still_silent(self) -> None:
        live = [_feature(1, _ID, geom_hash="same")]
        baseline = baseline_from_features(live)
        assert plan_local_changes(live, baseline) == []

    def test_a_row_with_no_id_is_still_a_create(self) -> None:
        changes = plan_local_changes([_feature(1, None)], {})
        assert [c.kind for c in changes] == ["create"]
        assert changes[0].portal_id

    def test_an_id_missing_from_the_baseline_still_keeps_its_id(self) -> None:
        """The resend-after-a-lost-response path, unchanged.

        A create whose response never arrived leaves the id in the file
        and nothing in the baseline. Minting a fresh id there would
        duplicate the feature on the next attempt, which is the exact
        thing the minting scheme exists to prevent.
        """
        changes = plan_local_changes([_feature(1, _ID)], {})
        assert changes[0].kind == "create"
        assert changes[0].portal_id == _ID

    def test_a_missing_row_is_still_a_delete(self) -> None:
        changes = plan_local_changes([], _baseline())
        assert [c.kind for c in changes] == ["delete"]
        assert changes[0].portal_id == _ID
