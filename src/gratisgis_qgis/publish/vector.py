# SPDX-License-Identifier: AGPL-3.0-or-later
"""Translate a QGIS vector layer description into a portal data_layer v3 schema.

Phase 3: "Publish vector layer". The flow the dialog runs:

  1. Pick a QGIS vector layer.
  2. Export it to a GeoPackage tempfile (canonical interchange
     format: preserves CRS, types, and the source-layer name).
  3. Stage the upload via ``client.ingest.stage(file_path=...)``.
     The portal probes the file and returns per-layer schema.
  4. Build a v3 data_layer ``data`` envelope from that probe with
     ``build_data_layer_envelope(...)``.
  5. Create the item via ``client.items.create(type="data_layer",
     data=envelope, ...)``.
  6. Enqueue one import job per layer via
     ``client.import_jobs.enqueue(...)``, then poll for progress.

This module owns the schema-translation rules. It's pure-Python so
the publish dialog can stay thin and the tests don't need QGIS.

QGIS-specific helpers (geometry-type mapping, attribute-type
mapping, GeoPackage export) live in ``ui/publish_vector_dialog.py``
so this file stays free of QGIS imports.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

# Portal-side v3 vocab. Mirrored here as string-literal types so we
# can fail loudly if the portal's vocab grows new values we haven't
# accounted for. (Tests pin this list.)
V3GeometryType = Literal["point", "line", "polygon"]
V3FieldType = Literal["string", "number", "boolean", "date"]


@dataclass(frozen=True)
class V3Field:
    """One attribute column in a portal data_layer layer."""

    name: str
    type: V3FieldType
    label: str = ""
    nullable: bool = True
    searchable: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "label": self.label or self.name,
            "nullable": self.nullable,
        }
        if self.searchable:
            # Only emit when True. The portal's engine indexing pass
            # opts in on this key (#23); omitting it preserves the
            # default-false semantic.
            out["searchable"] = True
        return out


@dataclass(frozen=True)
class V3Layer:
    """One layer in a portal data_layer v3 envelope.

    Multi-layer items hold several of these (e.g. a GDB with road
    centerlines + intersections + signs). For a single-vector-layer
    QGIS publish the envelope still wraps the one layer in a list,
    matching the v3 shape so the downstream consumer doesn't have
    to special-case single vs multi.
    """

    id: str
    """Stable per-item layer id. The portal uses this as part of
    the OGC collection-id (``<itemId>__<layerKey>``) so the value
    must be URL- and SQL-safe."""

    title: str
    """User-visible label. Defaults to id-titlecased if not given."""

    geometry_type: V3GeometryType | None
    """None for a non-spatial table layer. Spatial layers are one
    of the three v3 base types; multi-types get collapsed by the
    portal during ingest."""

    fields: list[V3Field] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # The portal ignores ``title`` on layer.to_dict for the
        # v3 schema (it lives on the parent item's title field for
        # single-layer items) but stores it for multi-layer items
        # as a layer-tree display label. Emit it always to keep
        # the multi-layer case correct.
        return {
            "id": self.id,
            "title": self.title or self.id,
            "geometryType": self.geometry_type,
            "fields": [f.to_dict() for f in self.fields],
        }


def build_data_layer_envelope(
    *,
    layers: Iterable[V3Layer],
) -> dict[str, Any]:
    """Compose the portal's data_layer v3 ``data`` envelope.

    The wrapper carries ``version=3``, which is the discriminant
    the portal's ``ItemsService.readV3Layers`` checks before any
    further processing. Bbox + feature counts come later from the
    ingest path; they're stamped onto the item by the worker.
    """
    layer_list = list(layers)
    return {
        "version": 3,
        "layers": [lyr.to_dict() for lyr in layer_list],
    }


# -----------------------------------------------------------
# QGIS-side normalization (still pure-Python: takes strings, not
# QGIS objects). The dialog runs these against QGIS layer state
# before constructing V3Layer dataclasses.
# -----------------------------------------------------------


# QGIS's QgsWkbTypes constants come back as strings like 'Point',
# 'MultiPolygon', 'LineString25D', 'PointZM'. We collapse them
# into the portal's three base types and drop the Z/M/Multi
# wrappers; the portal stores everything as Multi-* under the
# hood so the round-trip is lossless for the user's data.
_GEOMETRY_PREFIX = {
    "point": "point",
    "multipoint": "point",
    "line": "line",
    "linestring": "line",
    "multilinestring": "line",
    "multiline": "line",
    "polygon": "polygon",
    "multipolygon": "polygon",
    "curvepolygon": "polygon",
    "multicurve": "line",
    "multisurface": "polygon",
}


def qgis_geometry_to_v3(qgis_type: str) -> V3GeometryType | None:
    """Map a QGIS wkb-type name to the portal's base geometry type.

    Returns None for unknown / non-spatial types (the dialog should
    refuse to publish those as a spatial layer; the portal accepts
    table layers but the Phase 3 flow doesn't surface that path
    yet).
    """
    if not qgis_type:
        return None
    # Strip dimensionality suffixes (Z / M / ZM / 25D) and the
    # leading 'Multi' prefix. We lowercase first so the lookup
    # is case-insensitive across QGIS API revisions.
    stripped = qgis_type.lower()
    for suffix in ("zm", "25d", "z", "m"):
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return _GEOMETRY_PREFIX.get(stripped)


# QGIS QVariant type names → portal v3 field types. The portal
# enforces this vocab in tables.service.ts; values outside the
# union get rejected at item-create time.
_FIELD_TYPE_MAP = {
    # Strings
    "string": "string",
    "qstring": "string",
    "text": "string",
    # Numbers
    "int": "number",
    "integer": "number",
    "longlong": "number",
    "long": "number",
    "double": "number",
    "float": "number",
    "real": "number",
    # Booleans
    "bool": "boolean",
    "boolean": "boolean",
    # Dates
    "date": "date",
    "datetime": "date",
    "qdate": "date",
    "qdatetime": "date",
    "time": "date",
}


def qgis_field_type_to_v3(qgis_type: str) -> V3FieldType:
    """Normalize a QGIS QVariant type name to v3.

    Falls back to ``string`` for unrecognized types. The portal
    happily takes a string column and the user can type-convert
    it after the fact; a hard refusal here would be unfriendly for
    obscure source formats.
    """
    if not qgis_type:
        return "string"
    return _FIELD_TYPE_MAP.get(qgis_type.lower(), "string")  # type: ignore[return-value]


# Layer-id sanitization. The id ends up in URLs (the OGC
# collection-id is ``<itemId>__<layerKey>``) and in SQL identifiers
# (the engine names per-scope tables with it). Restrict to
# `[a-z0-9_]`, max 40 chars, can't start with a digit.
_ID_BAD_CHARS = re.compile(r"[^a-z0-9_]+")
_ID_LEADING_DIGIT = re.compile(r"^\d")


def sanitize_layer_id(raw: str, *, fallback: str = "layer") -> str:
    """Produce a portal-safe layer id from a user-facing string.

    Rules (matched to the portal's collection-id and SQL-identifier
    constraints):

      - lowercase
      - non-alphanumerics collapse to a single underscore
      - leading digit gets prefixed with ``l_``
      - length capped at 40
      - empty input falls back to ``fallback``
    """
    if not raw:
        return fallback
    s = raw.strip().lower()
    s = _ID_BAD_CHARS.sub("_", s).strip("_")
    if not s:
        return fallback
    if _ID_LEADING_DIGIT.match(s):
        s = "l_" + s
    if len(s) > 40:
        s = s[:40].rstrip("_") or fallback
    return s


def layer_from_probe(
    *,
    probe_layer: dict[str, Any],
    layer_id: str | None = None,
    title: str | None = None,
) -> V3Layer:
    """Translate one probe-response layer into a V3Layer.

    ``probe_layer`` is the dict shape the portal's ``/ingest/probe``
    returns per source layer (``name``, ``geometryType``, ``fields``,
    ``featureCount``). The optional ``layer_id`` overrides the
    auto-sanitized id (useful when the wizard lets the user rename).
    """
    raw_name = str(probe_layer.get("name", "")) or "layer"
    geom_raw = probe_layer.get("geometryType")
    geom: V3GeometryType | None
    if geom_raw is None:
        geom = None
    elif isinstance(geom_raw, str):
        # Probe returns the portal's vocab directly when invoked
        # via stage/probe (it's already been normalized server-side).
        # We still pass through qgis_geometry_to_v3 in case a future
        # probe path leaks raw QGIS / OGR type names.
        if geom_raw in ("point", "line", "polygon"):
            geom = geom_raw  # type: ignore[assignment]
        else:
            geom = qgis_geometry_to_v3(geom_raw)
    else:
        geom = None

    fields_raw = probe_layer.get("fields") or []
    fields: list[V3Field] = []
    for f in fields_raw:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        ftype_raw = str(f.get("type", "")).lower()
        if ftype_raw in ("string", "number", "boolean", "date"):
            ftype: V3FieldType = ftype_raw  # type: ignore[assignment]
        else:
            ftype = qgis_field_type_to_v3(ftype_raw)
        fields.append(V3Field(name=name, type=ftype, label=name))

    return V3Layer(
        id=layer_id or sanitize_layer_id(raw_name),
        title=title or raw_name,
        geometry_type=geom,
        fields=fields,
    )


# -----------------------------------------------------------
# Validation (pre-flight, run on the user's selection BEFORE we
# pay the bandwidth cost of staging the file).
# -----------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True)
class LayerSummary:
    """The slice of a QGIS layer the validator inspects.

    Populated by the dialog from ``QgsVectorLayer`` state so the
    validator doesn't import qgis.core.
    """

    name: str
    feature_count: int
    geometry_type: str
    """Raw QGIS wkb-type name (see qgis_geometry_to_v3)."""
    crs_auth_id: str
    """e.g. 'EPSG:4326', 'EPSG:26917', or '' for an undefined CRS."""
    is_valid: bool
    """Whether QGIS marked the underlying layer as valid (broken
    source path, missing provider, etc.)."""
    field_names: list[str] = field(default_factory=list)


def validate_layer(summary: LayerSummary) -> list[ValidationIssue]:
    """Return a list of issues blocking or warning the publish.

    Errors must be resolved before the dialog will let the user
    proceed; warnings get surfaced but don't gate the action.
    Designed to surface the failure modes that real-world QGIS
    users actually hit, not theoretical ones:

      - layer marked invalid by QGIS (broken source path)
      - 0 features (publishing an empty layer is allowed but warn)
      - unrecognized geometry type
      - missing CRS (we won't guess)
      - duplicate field names after normalization
      - field name that collides with a portal reserved column
    """
    issues: list[ValidationIssue] = []

    if not summary.is_valid:
        issues.append(
            ValidationIssue(
                severity="error",
                code="layer-invalid",
                message=(
                    "QGIS reports the layer as invalid. Fix the source "
                    "path or provider before publishing."
                ),
            )
        )

    if not summary.crs_auth_id:
        issues.append(
            ValidationIssue(
                severity="error",
                code="missing-crs",
                message=(
                    "Layer has no CRS defined. Assign a CRS in Layer "
                    "Properties and try again."
                ),
            )
        )

    geom = qgis_geometry_to_v3(summary.geometry_type)
    if geom is None:
        # We treat unknown-geometry as a warning, not an error: the
        # ingest path can still take a table layer.
        issues.append(
            ValidationIssue(
                severity="warning",
                code="non-spatial",
                message=(
                    f"Geometry type '{summary.geometry_type}' is not a "
                    "standard point/line/polygon. The layer will publish "
                    "as a table without a map preview."
                ),
            )
        )

    if summary.feature_count == 0:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="empty-layer",
                message=(
                    "Layer has zero features. The portal item will be "
                    "created but the map will be blank until you add data."
                ),
            )
        )

    # Field collisions after the portal's lowercasing pass.
    seen: dict[str, list[str]] = {}
    for name in summary.field_names:
        key = name.lower()
        seen.setdefault(key, []).append(name)
    for key, names in seen.items():
        if len(names) > 1:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="duplicate-field",
                    message=(
                        f"Field names {names!r} collide after case-"
                        "normalization. Rename one before publishing."
                    ),
                )
            )

    # Portal-reserved attribute column names. These are the ones the
    # engine adds itself; a user column with the same name would be
    # silently shadowed.
    reserved = {"id", "geom", "created_at", "edited_at", "created_by", "edited_by"}
    collisions = [n for n in summary.field_names if n.lower() in reserved]
    if collisions:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="reserved-field",
                message=(
                    f"Fields {collisions!r} use portal-reserved names. "
                    "They'll be renamed with a numeric suffix on import."
                ),
            )
        )

    return issues
