# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read a clone's current state through QGIS.

The one place that turns a QgsVectorLayer into the plain snapshots
``sync_state`` compares. Separated from that module so the diffing rules
stay testable without QGIS bindings, and separated from the dialog so
the same reading is available to the clone flow (which records the
baseline) and the sync flow (which compares against it).

Both callers going through here is load bearing rather than tidy. The
baseline is written by reading the GeoPackage back after it is created,
using exactly this function, so the two sides of every later comparison
were produced by the same code from the same container. Hashing the
downloaded GeoJSON instead would compare two encodings of the same
geometry and report a conflict on every untouched row.
"""
from __future__ import annotations

from typing import Any

from ..log import get_logger
from .sync_state import (
    PORTAL_EDITED_AT_PROPERTY,
    PORTAL_ID_FALLBACKS,
    BaselineEntry,
    LocalFeature,
    hash_attributes,
    hash_geometry,
)

_log = get_logger(__name__)


def read_local_features(layer: Any) -> list[LocalFeature]:
    """Snapshot every feature currently saved in a clone layer.

    Reads what is on disk. Anything sitting in QGIS's unsaved edit
    buffer is deliberately NOT included: a sync sends saved work only,
    so that answering "discard" in QGIS afterwards can never leave the
    portal holding something the local file does not have. That was the
    concrete failure of the edit-buffer design this replaces.
    """
    from ..offline.clone import PORTAL_ID_PROPERTY

    features: list[LocalFeature] = []
    for feature in layer.getFeatures():
        properties = _properties_of(feature)
        global_id = _global_id_of(properties, PORTAL_ID_PROPERTY)
        features.append(
            LocalFeature(
                qgis_fid=_fid_of(feature),
                global_id=global_id,
                attr_hash=hash_attributes(properties),
                geom_hash=hash_geometry(_geometry_wkb(feature)),
                geometry=_geometry_geojson(feature),
                properties=properties,
            )
        )
    return features


def baseline_from_features(
    features: list[LocalFeature],
    portal_edited_at: dict[str, str | None] | None = None,
) -> dict[str, BaselineEntry]:
    """Turn a snapshot into the baseline to store alongside it.

    Features with no id are skipped: the baseline is keyed by portal
    id, and a row without one has nothing on the portal to be a
    baseline for.
    """
    stamps = portal_edited_at or {}
    out: dict[str, BaselineEntry] = {}
    for feature in features:
        if not feature.global_id:
            continue
        out[feature.global_id] = BaselineEntry(
            attr_hash=feature.attr_hash,
            geom_hash=feature.geom_hash,
            portal_edited_at=stamps.get(feature.global_id),
        )
    return out


def portal_edited_stamps(feature_collection: Any) -> dict[str, str | None]:
    """Pull each portal feature's id and last-edited stamp.

    Verified against the live portal: every feature carries both, the
    top-level ``id`` equals ``properties._global_id``, and
    ``_edited_at`` is populated even on features nobody has edited
    (it equals ``_created_at`` there). That makes it a usable marker
    for "this row is not the one you cloned".
    """
    out: dict[str, str | None] = {}
    if not isinstance(feature_collection, dict):
        return out
    for raw in feature_collection.get("features") or []:
        if not isinstance(raw, dict):
            continue
        properties = raw.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        global_id = raw.get("id")
        for key in PORTAL_ID_FALLBACKS:
            if global_id:
                break
            global_id = properties.get(key)
        if not global_id:
            continue
        stamp = properties.get(PORTAL_EDITED_AT_PROPERTY)
        out[str(global_id)] = str(stamp) if stamp else None
    return out


# -----------------------------------------------------------
# QGIS feature accessors, each tolerant of a missing API
# -----------------------------------------------------------


def _properties_of(feature: Any) -> dict[str, Any]:
    try:
        names = [field.name() for field in feature.fields()]
    except Exception:
        return {}
    values = list(feature.attributes())
    return {
        name: values[index] if index < len(values) else None
        for index, name in enumerate(names)
    }


def _fid_of(feature: Any) -> int | None:
    try:
        return int(feature.id())
    except Exception:
        return None


def _geometry_wkb(feature: Any) -> bytes | None:
    try:
        geometry = feature.geometry()
    except Exception:
        return None
    if geometry is None:
        return None
    try:
        if geometry.isNull() or geometry.isEmpty():
            return None
        return bytes(geometry.asWkb())
    except Exception:
        _log.debug("could not read geometry as WKB", exc_info=True)
        return None


def _geometry_geojson(feature: Any) -> dict[str, Any] | None:
    """The geometry in the shape the portal's API expects."""
    import json

    try:
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            return None
        parsed = json.loads(geometry.asJson())
    except Exception:
        _log.debug("could not read geometry as GeoJSON", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None


def _global_id_of(properties: dict[str, Any], portal_id_property: str) -> str | None:
    for key in (portal_id_property, *PORTAL_ID_FALLBACKS):
        value = properties.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text != "NULL":
            return text
    return None
