# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline-clone helpers (Phase 7).

The flow:

  1. Dialog identifies a portal-backed layer (OAPIF source we
     recognize via `browser/uris.py`).
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
  - pre-flight validation of the chosen target directory.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal

# Match the portal's internal feature-id columns we strip out before
# writing to GeoPackage. These are useful for the push-edits flow
# (we honor them as the portal_id) but they round-trip into a local
# GeoPackage as duplicate id-like columns which confuses QGIS's fid
# inference. Keep the portal id under a single canonical name so the
# offline+edit+push round-trip is lossless.
_PORTAL_ID_PROPERTY = "_portal_id"


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

    We deliberately don't append a timestamp -- that would generate
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
        properties[_PORTAL_ID_PROPERTY] = portal_id
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
    candidates = (
        raw_feature.get("id"),
        properties.get("id"),
        properties.get("fid"),
        properties.get("feature_id"),
        properties.get("featureId"),
    )
    for value in candidates:
        if value is None or value == "":
            continue
        return str(value)
    return None


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
