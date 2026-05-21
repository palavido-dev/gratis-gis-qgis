# SPDX-License-Identifier: AGPL-3.0-or-later
"""Translate a QGIS edit buffer into portal feature-CRUD calls.

The Phase 4 "Push edits to portal" flow works like this:

  1. The user toggles edit mode on a layer that was added from
     the portal (OAPIF source we recognize via
     `browser/uris.py`).
  2. They make edits in QGIS: add features, move vertices, change
     attribute values, delete features.
  3. They hit "Push edits to portal" in the GratisGIS menu instead
     of QGIS's built-in Save Edits (which would round-trip through
     the OAPIF provider, and not all portals enable WFS-T).
  4. The dialog runs `build_sync_plan(...)` on this module to turn
     the edit buffer into a sequenced list of `SyncOp`s, then
     executes them against the portal via the client's
     `features` endpoint.

This module is pure-Python: callers pass plain dataclasses
describing what changed, and the planner emits the corresponding
API operations. Keeps the planning rules testable without QGIS.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

# Match the portal's feature-CRUD surface. Each SyncOp turns into
# exactly one HTTP call so retry semantics stay tractable.
SyncOpKind = Literal["create", "update", "delete"]


@dataclass(frozen=True)
class EditedFeature:
    """One in-progress edit captured from the QGIS edit buffer.

    The dialog builds these from `QgsVectorLayer.editBuffer()`:

      - addedFeatures()        -> kind='create', portal_id=None,
                                   geometry+properties from the new feat
      - changedGeometries()    -> kind='update', portal_id set,
                                   geometry set, properties=None
      - changedAttributeValues() -> kind='update', portal_id set,
                                   properties=dict of changes,
                                   geometry=None
      - deletedFeatureIds()    -> kind='delete', portal_id set,
                                   geometry=None, properties=None

    The planner merges co-occurring geometry+attribute edits to the
    same feature into a single PATCH so we don't pay 2x round-trips.
    """

    kind: SyncOpKind
    portal_id: str | None
    """Stable portal feature id. None for creates (the portal
    assigns one on the server)."""

    qgis_fid: int | None
    """The local QGIS feature id, used for the dialog's failure
    report so the user can find the offending row in QGIS."""

    geometry: dict[str, Any] | None = None
    """GeoJSON-shaped geometry. None means "don't change" on an
    update; on a create it means "no geometry" (table-layer add)."""

    properties: dict[str, Any] | None = None
    """Attribute map. On an update, only the keys present here
    are PATCHed; on a create, the full attribute set; on a
    delete, ignored."""


@dataclass(frozen=True)
class SyncOp:
    """One sequenced HTTP call against the portal."""

    kind: SyncOpKind
    qgis_fid: int | None
    portal_id: str | None
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None


@dataclass(frozen=True)
class SyncPlan:
    """The planner's output: ordered ops + a per-feature audit."""

    ops: list[SyncOp] = field(default_factory=list)
    """Execute in order; do not parallelize. Creates land first
    (so the portal assigns ids), then updates, then deletes. This
    ordering matches what the Survey123 + AGO mobile clients do
    and limits the failure surface: a partial run leaves the
    portal in a consistent state."""

    skipped: list[SkippedEdit] = field(default_factory=list)
    """Edits the planner refused (e.g. update on a feature with
    no portal_id, indicating it was created locally but never
    pushed). The dialog surfaces these so the user can fix the
    state and retry."""


@dataclass(frozen=True)
class SkippedEdit:
    qgis_fid: int | None
    reason: str


def build_sync_plan(edits: Iterable[EditedFeature]) -> SyncPlan:
    """Turn a list of edit-buffer entries into an ordered op list.

    Rules:

      - Create -> POST features (with the full geometry+properties).
      - Update -> PATCH features/:fid (geometry and/or properties).
      - Delete -> DELETE features/:fid.
      - Co-occurring geometry+attribute updates to the same portal_id
        merge into a single PATCH (one round-trip, atomic from the
        portal's perspective).
      - An update or delete with no portal_id is skipped with reason
        "no portal id" (the row exists only locally).
      - A create with neither geometry nor properties is skipped with
        reason "empty create" (would be a no-op POST).
      - Order: all creates, then all updates, then all deletes. Within
        a kind we preserve input order so the dialog's progress UX
        feels stable across runs.
    """
    creates: list[SyncOp] = []
    # Keyed by portal_id so we can merge co-occurring geom+attr edits.
    updates: dict[str, SyncOp] = {}
    deletes: list[SyncOp] = []
    skipped: list[SkippedEdit] = []

    for e in edits:
        if e.kind == "create":
            if e.geometry is None and not e.properties:
                skipped.append(
                    SkippedEdit(
                        qgis_fid=e.qgis_fid,
                        reason=(
                            "Empty create: feature has no geometry and no "
                            "attribute values."
                        ),
                    )
                )
                continue
            creates.append(
                SyncOp(
                    kind="create",
                    qgis_fid=e.qgis_fid,
                    portal_id=None,
                    geometry=e.geometry,
                    properties=e.properties,
                )
            )
            continue

        if e.kind in ("update", "delete") and not e.portal_id:
            skipped.append(
                SkippedEdit(
                    qgis_fid=e.qgis_fid,
                    reason=(
                        "Feature has no portal id; create the feature on "
                        "the portal before editing or deleting it."
                    ),
                )
            )
            continue

        if e.kind == "update":
            assert e.portal_id is not None  # for type-checker
            if e.geometry is None and not e.properties:
                # An update entry with neither delta is a no-op edit;
                # the QGIS buffer occasionally produces these during
                # transient selection toggles. Drop quietly.
                continue
            existing = updates.get(e.portal_id)
            if existing is None:
                updates[e.portal_id] = SyncOp(
                    kind="update",
                    qgis_fid=e.qgis_fid,
                    portal_id=e.portal_id,
                    geometry=e.geometry,
                    properties=dict(e.properties) if e.properties else None,
                )
            else:
                # Merge into the existing op. The merge favors the
                # later entry on key collisions because the QGIS edit
                # buffer reports edits in the order the user made
                # them, so the latest value wins.
                merged_props: dict[str, Any] | None = None
                if existing.properties or e.properties:
                    merged_props = dict(existing.properties or {})
                    if e.properties:
                        merged_props.update(e.properties)
                merged_geom = e.geometry if e.geometry is not None else existing.geometry
                updates[e.portal_id] = SyncOp(
                    kind="update",
                    qgis_fid=e.qgis_fid,
                    portal_id=e.portal_id,
                    geometry=merged_geom,
                    properties=merged_props,
                )
            continue

        if e.kind == "delete":
            assert e.portal_id is not None  # for type-checker
            # If we previously planned an update for this id, drop
            # the update -- the delete supersedes it. Saves one
            # round-trip and avoids a 404 on the PATCH if the user
            # both edited and then deleted the same feature.
            updates.pop(e.portal_id, None)
            deletes.append(
                SyncOp(
                    kind="delete",
                    qgis_fid=e.qgis_fid,
                    portal_id=e.portal_id,
                )
            )
            continue

    return SyncPlan(
        ops=[*creates, *updates.values(), *deletes],
        skipped=skipped,
    )


def summarize_plan(plan: SyncPlan) -> str:
    """Render a one-line summary the dialog can put in its
    confirmation prompt. Order matches build_sync_plan's emit order
    so the displayed numbers add up.
    """
    creates = sum(1 for op in plan.ops if op.kind == "create")
    updates = sum(1 for op in plan.ops if op.kind == "update")
    deletes = sum(1 for op in plan.ops if op.kind == "delete")
    return (
        f"{creates} create(s), {updates} update(s), {deletes} delete(s); "
        f"{len(plan.skipped)} skipped."
    )
