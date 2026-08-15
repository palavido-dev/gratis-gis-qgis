# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline-clone helpers (Phase 7).

The flow:

  1. Dialog identifies a portal-backed layer (any source shape
     `browser/uris.py` can resolve to an item + layer).
  2. Plugin downloads a full GeoJSON FeatureCollection via the
     client's `features.download_geojson(...)`.
  3. Plugin normalizes the response and writes it to a local
     GeoPackage via QGIS's QgsVectorFileWriter.
  4. Plugin loads the GeoPackage as a new QGIS layer so the user
     can keep working when offline.

This module owns the parts of the pipeline that don't touch QGIS:

  - destination-path generation (deterministic, collision-safe)
  - GeoJSON shape normalization (so an empty or malformed body
    doesn't propagate as a `None` into the writer)
  - pre-flight validation of the chosen target directory
  - reading back the portal origin the writer stamps into the
    GeoPackage, which is what makes a clone pushable.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.request import pathname2url

from ..browser.uris import PortalLayerRef
from ..log import get_logger
from .sync_state import (
    BASELINE_FIELDS,
    BASELINE_TABLE,
    PORTAL_ID_FALLBACKS,
    BaselineEntry,
)

_log = get_logger(__name__)

# Match the portal's internal feature-id columns we strip out before
# writing to GeoPackage. These are useful for the push-edits flow
# (we honor them as the portal_id) but they round-trip into a local
# GeoPackage as duplicate id-like columns which confuses QGIS's fid
# inference. Keep the portal id under a single canonical name so the
# offline+edit+push round-trip is lossless. Public because the
# push-edits flow reads AND writes this column (created features get
# their portal-assigned id written back after a push).
PORTAL_ID_PROPERTY = "_portal_id"


@dataclass(frozen=True)
class CloneTarget:
    """Where the downloaded layer should land on disk."""

    directory: str
    """Absolute path to the destination folder."""

    file_name: str
    """Basename without extension. Sanitized for cross-platform use."""

    @property
    def gpkg_path(self) -> str:
        return os.path.join(self.directory, f"{self.file_name}.gpkg")


def make_target(
    *,
    directory: str,
    item_title: str,
    layer_id: str,
) -> CloneTarget:
    """Build a CloneTarget with a portable, collision-safe filename.

    Filename rules:
      - lowercase
      - non-alnum collapses to single underscore
      - leading digits get prefixed with `clone_`
      - capped at 80 chars (room for the .gpkg suffix + Windows path budget)
      - falls back to ``clone_<layer_id>`` if the title produces an empty stem

    We deliberately don't append a timestamp, which would generate
    a new file on every clone and pile up. The caller can collision-
    handle (overwrite confirm, suffix-N) at the dialog layer.
    """
    stem = _sanitize_stem(item_title)
    if not stem:
        stem = f"clone_{_sanitize_stem(layer_id) or 'layer'}"
    return CloneTarget(directory=directory, file_name=stem)


def normalize_feature_collection(body: Any) -> dict[str, Any]:
    """Coerce the portal's geojson response into something writable.

    The portal returns a well-formed FeatureCollection on success;
    this helper handles three failure modes the dialog otherwise
    has to repeat the logic for:

      - non-dict body (proxy error, network corruption): return an
        empty FeatureCollection so the writer emits a 0-row GPKG.
      - missing ``features`` key: same treatment.
      - features list with malformed entries: filter them out and
        keep going so one bad row doesn't break the whole clone.

    Also normalizes the per-feature shape: any portal-internal id
    column collides with QGIS's auto-assigned fid, so we move
    those into a single ``_portal_id`` property the push-edits
    flow recognizes on the round-trip.
    """
    if not isinstance(body, dict):
        return {"type": "FeatureCollection", "features": []}
    features_raw = body.get("features")
    if not isinstance(features_raw, list):
        return {"type": "FeatureCollection", "features": []}

    cleaned: list[dict[str, Any]] = []
    for f in features_raw:
        normalized = _normalize_feature(f)
        if normalized is not None:
            cleaned.append(normalized)
    return {
        "type": "FeatureCollection",
        "features": cleaned,
    }


def _normalize_feature(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    feat_type = raw.get("type", "Feature")
    if feat_type != "Feature":
        # The portal never returns Feature children of a different
        # type; bail rather than guessing.
        return None
    geometry = raw.get("geometry")
    properties = raw.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}

    # Move portal-internal id fields under _portal_id so the local
    # GeoPackage's fid stays QGIS-managed.
    portal_id = _extract_portal_id(raw, properties)
    if portal_id is not None:
        properties = dict(properties)
        properties[PORTAL_ID_PROPERTY] = portal_id
        # Drop the source aliases so they don't survive as
        # separate columns (would confuse a future re-clone).
        for alias in ("id", "fid", "feature_id", "featureId"):
            properties.pop(alias, None)

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": properties,
    }


def _extract_portal_id(
    raw_feature: dict[str, Any], properties: dict[str, Any]
) -> str | None:
    """Pull the portal feature id from the most reliable source.

    Order matches what the push-edits flow looks at, so the round-
    trip is lossless: id-on-feature first, then properties variants.
    """
    # `_global_id` first among the property fallbacks because it is
    # what the portal actually sends; it was missing from this list
    # entirely. The top-level `id` stays ahead of it only because the
    # portal sets the two to the same value, verified across a whole
    # layer, so preferring either is correct and `id` is cheaper.
    candidates = (
        raw_feature.get("id"),
        *(properties.get(key) for key in PORTAL_ID_FALLBACKS),
    )
    for value in candidates:
        if value is None or value == "":
            continue
        return str(value)
    return None


# -----------------------------------------------------------
# Safe replacement of the clone target.
# -----------------------------------------------------------


@contextlib.contextmanager
def safe_write_path(
    final_path: str, *, allow_in_place: bool = False
) -> Iterator[str]:
    """Yield a temp path beside ``final_path``; promote on success.

    The writer inside the ``with`` block gets a sibling temp path in
    the same directory, so the final ``os.replace`` is an atomic
    same-filesystem rename. Only when the block completes without
    raising does the temp file replace the target; on failure the
    temp file is removed and the existing target, if any, stays
    untouched. This is what keeps a failed re-clone from destroying
    the user's previous (possibly locally edited) offline copy, which
    the old unlink-the-target-then-write sequence did.

    ``allow_in_place`` permits the fallback in ``_promote`` for a
    target Windows will not let us rename over. It is off by default
    and has to be asked for, because it cannot tell a file held open by
    a stale GDAL pool entry from one a live layer is reading right now,
    and overwriting the second is data corruption. Only a caller that
    has already closed everything reading the file may turn it on.
    """
    directory = os.path.dirname(final_path) or "."
    basename = os.path.basename(final_path)
    # A temp DIRECTORY holding the real filename, rather than a temp
    # file beside the target. Two reasons, both learned the hard way:
    # mkstemp CREATES the file, and OGR cannot create a GeoPackage on
    # top of an existing zero-byte file, so the write silently produced
    # nothing; and the driver cares about the extension, which a
    # ".part" suffix destroys. Keeping the final basename also means
    # the layer inside the container is named after the target rather
    # than a random temp stem.
    tmp_dir = tempfile.mkdtemp(prefix=f".{basename}.", dir=directory)
    tmp_path = os.path.join(tmp_dir, basename)
    try:
        yield tmp_path
        _promote(tmp_path, final_path, tmp_dir, allow_in_place=allow_in_place)
    finally:
        # finally, not else: the promote itself can fail, and an
        # earlier version cleaned up only on the paths it had thought
        # of. It left a hidden staging directory in the user's chosen
        # folder every time an overwrite was refused.
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _promote(
    tmp_path: str, final_path: str, tmp_dir: str, *, allow_in_place: bool
) -> None:
    """Move the staged file into place, by rename or by contents.

    The rename is the good path: same filesystem, so it is atomic, and
    a half-finished write can never be visible at the target.

    Windows refuses it when anything still holds the target open, and
    on a re-clone something does. Reading a GeoPackage through QGIS
    puts the dataset in GDAL's pool, and the pool does NOT release it
    when the layer is removed from the project. Measured, because the
    distinction is not guessable: a layer that was merely opened
    releases the file, a layer whose features have been read does not,
    and every layer drawn on the canvas has read its features. That is
    why an overwrite worked in a headless test and failed on the second
    real clone.

    Waiting does not help; the lock is not transient. Repointing the
    provider elsewhere before removing the layer does not help either.
    Asking GDAL to flush its pool by destroying the driver manager
    takes the whole process down with an access violation.

    What does work is writing the bytes into the existing file rather
    than replacing the file itself: the handle permits writes, it is
    only the rename that is refused. That costs the atomicity, so the
    existing contents are copied aside first and put back if the write
    fails part way. Reading the locked file is allowed, which is what
    makes that backup possible.
    """
    try:
        os.replace(tmp_path, final_path)
        return
    except OSError as rename_error:
        if not allow_in_place or not os.path.exists(final_path):
            # Either the caller has not vouched that nothing is reading
            # the target, or the target does not exist, in which case
            # the rename failed for some other reason and guessing
            # would only hide it.
            raise
        _log.info(
            "could not rename over %s (%s); writing the contents in place",
            final_path,
            rename_error,
        )

    backup = os.path.join(tmp_dir, "previous.bak")
    shutil.copyfile(final_path, backup)
    try:
        with open(tmp_path, "rb") as staged, open(final_path, "r+b") as target:
            shutil.copyfileobj(staged, target)
            target.truncate()
    except BaseException:
        # Put back what was there. Without this the fallback would be
        # strictly worse than the failure it replaces: a refused
        # overwrite leaves the user's copy intact, a half-written one
        # destroys it.
        with contextlib.suppress(OSError):
            shutil.copyfile(backup, final_path)
        raise


def source_targets_file(source: str, path: str) -> bool:
    """Whether a QGIS layer source refers to the file at ``path``.

    An OGR source is ``<file>|layername=<name>``, sometimes with more
    pipe-separated options, so the filename is everything up to the
    first pipe. Compared through ``normcase`` because a user typing a
    destination gets a different drive-letter case than QGIS records,
    and on Windows those are the same file.

    Needed because a GeoPackage open in the project cannot be replaced
    on Windows: the overwrite has to find the layers holding it first.
    """
    if not source or not path:
        return False
    candidate = source.split("|", 1)[0].strip()
    if not candidate:
        return False
    try:
        return os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(
            os.path.abspath(path)
        )
    except (OSError, ValueError):
        return False


# -----------------------------------------------------------
# Clone provenance: which portal layer a GeoPackage came from.
# -----------------------------------------------------------

# A clone is a plain GeoPackage, so nothing in its source URI says
# which portal layer it came from and the push-edits dialog could
# never offer it back. The origin is recorded as an extra
# (non-spatial) table INSIDE the container rather than a sidecar
# file: a sidecar is lost the moment someone moves, copies or emails
# the .gpkg, while a table travels with the data.
CLONE_SOURCE_TABLE = "gratisgis_source"

# Column order is the writer's field order too; both sides read this
# tuple so they cannot drift apart.
CLONE_SOURCE_FIELDS = ("portal_url", "item_id", "layer_id", "cloned_at")


def clone_timestamp() -> str:
    """ISO-8601 UTC stamp for the clone-source row."""
    return datetime.now(timezone.utc).isoformat()


def read_clone_source(gpkg_path: str) -> PortalLayerRef | None:
    """Recover the portal origin recorded in a cloned GeoPackage.

    Returns None whenever the file is not a clone of ours (no such
    table, unreadable, empty row). Never raises: this runs while a
    dialog populates its layer list, over every GeoPackage in the
    user's project, most of which have nothing to do with the portal.

    Read through sqlite3 rather than a QGIS provider because a
    GeoPackage is a SQLite database by definition, this needs no
    geometry support, and staying QGIS-free keeps the reader usable
    (and testable) outside a running QGIS. Opened read-only via a
    URI filename so probing an unrelated path can neither create a
    file nor take a write lock on one the user has open.
    """
    if not gpkg_path or not os.path.isfile(gpkg_path):
        return None
    columns = ", ".join(f'"{name}"' for name in CLONE_SOURCE_FIELDS[:3])
    try:
        uri = f"file:{pathname2url(os.path.abspath(gpkg_path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                f'SELECT {columns} FROM "{CLONE_SOURCE_TABLE}" LIMIT 1'
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        # An ordinary GeoPackage simply has no such table; that is the
        # expected path here, not an error worth surfacing.
        _log.debug("no clone-source table in %s", gpkg_path, exc_info=True)
        return None
    if not row:
        return None
    portal_url, item_id, layer_id = (str(v or "") for v in row)
    if not portal_url or not item_id or not layer_id:
        return None
    return PortalLayerRef(portal_url=portal_url, item_id=item_id, layer_id=layer_id)


# -----------------------------------------------------------
# The baseline: what the clone looked like when it was taken.
# -----------------------------------------------------------


def write_baseline(
    gpkg_path: str, entries: Mapping[str, BaselineEntry]
) -> None:
    """Record (or replace) the clone's baseline table.

    Written through sqlite3 rather than a QGIS writer because it is a
    plain attribute table, and because QgsVectorFileWriter would need a
    memory layer, a field spec and an enum dance to say something this
    simple. A GeoPackage is a SQLite database by definition.

    Replaces the table wholesale: after a successful sync the baseline
    should describe the new state exactly, and reconciling row by row
    would leave stale entries behind on any path that missed one.
    """
    columns = ", ".join(f'"{name}" TEXT' for name in BASELINE_FIELDS)
    placeholders = ", ".join("?" for _ in BASELINE_FIELDS)
    conn = sqlite3.connect(gpkg_path)
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{BASELINE_TABLE}"')
        conn.execute(f'CREATE TABLE "{BASELINE_TABLE}" ({columns})')
        conn.executemany(
            f'INSERT INTO "{BASELINE_TABLE}" VALUES ({placeholders})',
            [
                (gid, entry.attr_hash, entry.geom_hash, entry.portal_edited_at)
                for gid, entry in entries.items()
            ],
        )
        conn.commit()
    finally:
        conn.close()


def read_baseline(gpkg_path: str) -> dict[str, BaselineEntry]:
    """Read back the clone's baseline, or {} if it has none.

    An empty result is meaningful and is NOT the same as "no changes":
    a clone written before baselines existed has no table, and every
    one of its features would then look newly created. Callers must
    distinguish the two, which is what ``has_baseline`` is for.
    """
    if not gpkg_path or not os.path.isfile(gpkg_path):
        return {}
    columns = ", ".join(f'"{name}"' for name in BASELINE_FIELDS)
    try:
        uri = f"file:{pathname2url(os.path.abspath(gpkg_path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(
                f'SELECT {columns} FROM "{BASELINE_TABLE}"'
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        _log.debug("no baseline table in %s", gpkg_path, exc_info=True)
        return {}
    out: dict[str, BaselineEntry] = {}
    for global_id, attr_hash, geom_hash, edited_at in rows:
        if not global_id:
            continue
        out[str(global_id)] = BaselineEntry(
            attr_hash=str(attr_hash or ""),
            geom_hash=str(geom_hash or ""),
            portal_edited_at=str(edited_at) if edited_at else None,
        )
    return out


def has_baseline(gpkg_path: str) -> bool:
    """Whether this GeoPackage carries a baseline table at all.

    Distinguishes "cloned, nothing changed yet" from "made by a version
    that did not record one". The second cannot be synced safely,
    because with no baseline every existing feature reads as new and a
    sync would duplicate the entire layer on the portal.
    """
    if not gpkg_path or not os.path.isfile(gpkg_path):
        return False
    try:
        uri = f"file:{pathname2url(os.path.abspath(gpkg_path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (BASELINE_TABLE,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


# -----------------------------------------------------------
# Pre-flight validation of the target directory.
# -----------------------------------------------------------


@dataclass(frozen=True)
class CloneValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


def validate_clone_target(target: CloneTarget) -> list[CloneValidationIssue]:
    """Run the local checks the dialog can do before downloading.

    Catches the most common foot-guns: directory doesn't exist,
    isn't writable, target file already exists (warn so the dialog
    can ask to overwrite).
    """
    issues: list[CloneValidationIssue] = []
    if not target.directory:
        issues.append(
            CloneValidationIssue(
                severity="error",
                code="no-directory",
                message="No destination directory chosen.",
            )
        )
        return issues
    if not os.path.isdir(target.directory):
        issues.append(
            CloneValidationIssue(
                severity="error",
                code="directory-missing",
                message=f"Directory does not exist: {target.directory}",
            )
        )
        return issues
    if not os.access(target.directory, os.W_OK):
        issues.append(
            CloneValidationIssue(
                severity="error",
                code="directory-not-writable",
                message=f"Directory is not writable: {target.directory}",
            )
        )
    if os.path.exists(target.gpkg_path):
        issues.append(
            CloneValidationIssue(
                severity="warning",
                code="target-exists",
                message=(
                    f"File already exists and will be overwritten: "
                    f"{os.path.basename(target.gpkg_path)}"
                ),
            )
        )
    return issues


# -----------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------


_BAD_CHARS = re.compile(r"[^a-z0-9_]+")
_LEADING_DIGIT = re.compile(r"^\d")


def _sanitize_stem(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip().lower()
    s = _BAD_CHARS.sub("_", s).strip("_")
    if not s:
        return ""
    if _LEADING_DIGIT.match(s):
        s = "clone_" + s
    if len(s) > 80:
        s = s[:80].rstrip("_") or ""
    return s
