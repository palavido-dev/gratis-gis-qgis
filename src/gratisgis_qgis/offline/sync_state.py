# SPDX-License-Identifier: AGPL-3.0-or-later
"""Track what changed in an offline clone, durably.

The first version of the push flow read QGIS's pending edit buffer. That
was wrong in a way that mattered:

  - Edits only existed to the plugin while UNSAVED, so the ordinary
    habit of saving as you go through a long session made them
    invisible. The user had to be told not to save, which is a strange
    thing to ask.
  - Nothing recorded what had been pushed. Pushing from the buffer and
    then answering "discard" to QGIS left the portal holding changes
    the local file never had, with nothing anywhere aware of it.
  - Closing QGIS lost the lot.

So the state moves into the clone itself. A baseline table records what
each feature looked like when it was cloned; the difference between the
GeoPackage on disk and that baseline IS the set of pending changes. That
survives saving, closing QGIS, reopening tomorrow, and even emailing the
file to someone else, because it is all inside the one container.

This module is pure Python. It knows nothing about QGIS or HTTP: callers
hand it feature snapshots and it says what should be sent. The QGIS-side
reading and the sqlite-side storage live in ``offline/clone.py`` and the
sync dialog.

Two independent hash families, deliberately
-------------------------------------------
Local change is detected by hashing the GeoPackage's own representation
of a feature. Portal change is detected from the ``_edited_at`` stamp the
portal puts on every feature. They are never compared with each other.

That split is the point. Hashing the portal's GeoJSON and comparing it
against a hash taken from the GeoPackage would compare two different
encodings of the same geometry, and the float formatting alone would
report a conflict on every untouched row. Each side is compared only
against a value recorded in the same representation.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..edit.sync import EditedFeature

#: Table inside the clone GeoPackage holding the as-cloned state.
#: A table rather than a sidecar file for the same reason the origin
#: record is: a sidecar is lost the moment the .gpkg is moved or sent
#: to someone, and then the clone silently looks like it has no
#: pending changes at all.
BASELINE_TABLE = "gratisgis_baseline"

#: Column order is the writer's field order too; both sides read this
#: tuple so they cannot drift apart.
BASELINE_FIELDS = ("global_id", "attr_hash", "geom_hash", "portal_edited_at")

#: Property names that belong to the portal or to the local container
#: rather than to the user's data. Excluded from the attribute hash so
#: that re-reading a feature the portal has restamped does not read as
#: a local edit.
#:
#: ``fid`` is GeoPackage's own row id, and ``_portal_id`` is the column
#: the clone writes the portal's feature id into; neither is user data.
#: The portal's audit stamps are excluded for the same reason and
#: because they change on every server-side edit, which is precisely
#: what the portal-side check handles separately.
INTERNAL_PROPERTIES = frozenset(
    {
        "fid",
        "_portal_id",
        "_global_id",
        "_created_at",
        "_created_by",
        "_edited_at",
        "_edited_by",
    }
)

#: The portal's per-feature last-edited stamp. Verified present on
#: every feature the portal returns, including ones nobody has edited,
#: where it equals the created stamp. This is the only server-side
#: change marker available: the write routes accept no version token
#: and the read routes expose no revision, so a stamp comparison is
#: what conflict detection has to be built on.
PORTAL_EDITED_AT_PROPERTY = "_edited_at"

#: Where a feature's portal id may be found, after the top-level ``id``.
#: ``_global_id`` is what the portal actually sends and was missing from
#: the original list, which is worth keeping first here.
PORTAL_ID_FALLBACKS = ("_global_id", "id", "fid", "feature_id", "featureId")


def new_global_id() -> str:
    """Mint a stable id for a locally created feature.

    Client-minted, and written into the clone BEFORE the create is
    sent, which is what makes a retry safe: the portal dedupes an
    append on ``globalId``, so a create that fails halfway (or whose
    response is lost) can be resent without producing a second copy.
    Letting the server mint the id would make that impossible to get
    right, because a lost response would be indistinguishable from a
    rejected request.
    """
    return str(uuid.uuid4())


# -----------------------------------------------------------
# Hashing
# -----------------------------------------------------------


def hash_attributes(properties: Mapping[str, Any] | None) -> str:
    """Hash the user-meaningful attributes of a feature.

    Canonical JSON with sorted keys, so a dict-ordering difference
    between two reads cannot read as an edit.
    """
    payload = {
        key: _jsonable(value)
        for key, value in sorted((properties or {}).items())
        if key not in INTERNAL_PROPERTIES
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def hash_geometry(wkb: bytes | None) -> str:
    """Hash a geometry from its WKB.

    WKB rather than WKT or GeoJSON because it is the one form with no
    text formatting in it: no decimal rounding, no locale, no
    whitespace. Two reads of an unedited geometry hash identically.

    The empty string stands for "no geometry", which a table row and a
    feature whose geometry was cleared both legitimately are.
    """
    if not wkb:
        return ""
    return hashlib.sha256(wkb).hexdigest()[:32]


def _jsonable(value: Any) -> Any:
    """Coerce a QGIS attribute value into something json can encode.

    QGIS hands back QDate/QDateTime/QVariant-ish objects and its own
    NULL sentinel. Anything unrecognised falls through to ``str`` via
    the encoder's ``default``, which is fine here: the hash only has to
    be stable, not reversible.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    # QGIS's NULL is a QVariant that stringifies to "NULL"; treating it
    # as None keeps it equal to a genuine null read back from sqlite.
    text = str(value)
    return None if text == "NULL" else text


# -----------------------------------------------------------
# The two sides of the comparison
# -----------------------------------------------------------


@dataclass(frozen=True)
class BaselineEntry:
    """What one feature looked like when the clone was made."""

    attr_hash: str
    geom_hash: str
    portal_edited_at: str | None = None


@dataclass(frozen=True)
class LocalFeature:
    """One feature as it stands in the clone GeoPackage right now.

    ``global_id`` is None for a row the user added locally that has
    never been given an id.
    """

    qgis_fid: int | None
    global_id: str | None
    attr_hash: str
    geom_hash: str
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None


@dataclass(frozen=True)
class Conflict:
    """A feature both sides changed since the clone was taken."""

    global_id: str
    qgis_fid: int | None
    kind: str
    """What the local side wants to do: ``update`` or ``delete``."""

    detail: str
    """Plain wording for the dialog."""


# -----------------------------------------------------------
# Diffing
# -----------------------------------------------------------


def plan_local_changes(
    live: Iterable[LocalFeature],
    baseline: Mapping[str, BaselineEntry],
) -> list[EditedFeature]:
    """Work out what changed locally since the clone was taken.

    Rules, in the order they are decided:

      - A row with no id is new: mint one, create it.
      - A row claiming an id an EARLIER row already claimed is new too,
        and gets a fresh id rather than the one it arrived with. Two
        local rows cannot both be one portal feature. This is what
        splitting a polygon produces: QGIS keeps the original feature
        and adds another for the new part, copying every attribute
        across, portal id included. Without this rule the two collapse
        downstream, in two places at once, and both are silent. The
        plan builder merges updates by portal id, so only one half ever
        reaches the portal and the other disappears. The baseline is
        keyed by portal id too, so the surviving entry can only match
        one of the rows and the other reads as edited forever.

        The first row seen keeps the id. Features arrive in fid order,
        so that is the original feature, and the piece QGIS added
        becomes the new one. Either choice sends the same two
        geometries; this one keeps the portal's existing feature
        pointing at the part that kept its identity locally.
      - A row whose id is not in the baseline is also a create. That is
        not a contradiction: an id is written into the clone at the
        moment it is minted, BEFORE the create is sent, so this is
        exactly the state a create that failed (or whose response never
        arrived) leaves behind. Resending is safe because the portal
        dedupes on the id, and it is the only way a lost response
        recovers.
      - A row whose hashes match the baseline is untouched, and is not
        mentioned again.
      - A row whose hashes differ is an update. The whole property set
        is sent, not a delta, matching what the portal's own field app
        does and what the PATCH route expects.
      - A baseline id with no live row was deleted.

    Deletes are derived from absence, so a feature removed and saved is
    caught even though nothing was watching when it happened. That is
    the whole reason the baseline exists rather than a change log.
    """
    changes: list[EditedFeature] = []
    seen: set[str] = set()

    for feature in live:
        global_id = feature.global_id or None
        # Checked before it is recorded as seen, or every row would
        # look like a duplicate of itself.
        duplicate = global_id is not None and global_id in seen
        if global_id is not None:
            seen.add(global_id)
        recorded = (
            None
            if duplicate or global_id is None
            else baseline.get(global_id)
        )

        if recorded is None:
            changes.append(
                EditedFeature(
                    kind="create",
                    # A duplicate must NOT keep the id it copied: that
                    # is the collision, and reusing it would send the
                    # create straight back into the same merge.
                    portal_id=(
                        new_global_id()
                        if duplicate or global_id is None
                        else global_id
                    ),
                    qgis_fid=feature.qgis_fid,
                    geometry=feature.geometry,
                    properties=_user_properties(feature.properties),
                )
            )
            continue

        if (
            recorded.attr_hash == feature.attr_hash
            and recorded.geom_hash == feature.geom_hash
        ):
            continue

        changes.append(
            EditedFeature(
                kind="update",
                portal_id=global_id,
                qgis_fid=feature.qgis_fid,
                geometry=feature.geometry,
                properties=_user_properties(feature.properties),
            )
        )

    for global_id in baseline:
        if global_id not in seen:
            changes.append(
                EditedFeature(
                    kind="delete",
                    portal_id=global_id,
                    qgis_fid=None,
                )
            )
    return changes


def _user_properties(properties: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip the columns that are ours, not the user's.

    Sending ``_portal_id`` or ``fid`` back would create a portal
    attribute named after a local bookkeeping column.
    """
    return {
        key: _jsonable(value)
        for key, value in (properties or {}).items()
        if key not in INTERNAL_PROPERTIES
    }


def find_conflicts(
    changes: Iterable[EditedFeature],
    baseline: Mapping[str, BaselineEntry],
    portal_edited_at: Mapping[str, str | None],
) -> list[Conflict]:
    """Find features that moved on BOTH sides since the clone.

    ``portal_edited_at`` is the portal's current per-feature stamp,
    keyed by id, read immediately before sending. A feature missing
    from it no longer exists on the portal.

    This is the whole conflict story, and it is deliberately narrow.
    The portal has no version token on a write and no compare-and-set,
    so nothing can stop a clobber at the moment of writing; what CAN be
    done is to notice beforehand that the server row is not the one
    that was cloned, and say so rather than overwrite in silence. A
    local change to a row nobody else touched is not a conflict and is
    never reported as one.

    Creates are never conflicts: a feature the portal has not seen
    cannot have been changed there.
    """
    conflicts: list[Conflict] = []
    for change in changes:
        if change.kind == "create" or not change.portal_id:
            continue
        recorded = baseline.get(change.portal_id)
        if recorded is None:
            continue

        if change.portal_id not in portal_edited_at:
            if change.kind == "delete":
                # Both sides deleted it. Agreement, not conflict.
                continue
            conflicts.append(
                Conflict(
                    global_id=change.portal_id,
                    qgis_fid=change.qgis_fid,
                    kind=change.kind,
                    detail="Deleted on the portal since you cloned it.",
                )
            )
            continue

        current = portal_edited_at[change.portal_id]
        if recorded.portal_edited_at is None or current is None:
            # Nothing to compare against. Do not invent a conflict.
            continue
        if current != recorded.portal_edited_at:
            verb = "change" if change.kind == "update" else "delete"
            conflicts.append(
                Conflict(
                    global_id=change.portal_id,
                    qgis_fid=change.qgis_fid,
                    kind=change.kind,
                    detail=(
                        f"Changed on the portal since you cloned it; your "
                        f"{verb} would overwrite that."
                    ),
                )
            )
    return conflicts


def summarize_changes(changes: Iterable[EditedFeature]) -> str:
    """One line for the dialog, in plain words."""
    counts = {"create": 0, "update": 0, "delete": 0}
    for change in changes:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    if not any(counts.values()):
        return "No changes to send."
    parts = []
    for kind, label in (("create", "added"), ("update", "changed"), ("delete", "removed")):
        if counts[kind]:
            parts.append(f"{counts[kind]} {label}")
    return ", ".join(parts) + "."
