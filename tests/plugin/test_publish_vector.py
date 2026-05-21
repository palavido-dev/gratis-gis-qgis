# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the publish-vector-layer translator + validator.

These cover the pure-Python pieces that turn a QGIS layer
description into the portal's v3 data_layer envelope. QGIS-side
helpers (file export, layer-tree iteration) live in the dialog
and are exercised by manual smoke against a running QGIS.
"""
from __future__ import annotations

from typing import Any

import pytest

from gratisgis_qgis.publish.vector import (
    LayerSummary,
    V3Field,
    V3Layer,
    ValidationIssue,
    build_data_layer_envelope,
    layer_from_probe,
    qgis_field_type_to_v3,
    qgis_geometry_to_v3,
    sanitize_layer_id,
    validate_layer,
)


class TestGeometryMapping:
    @pytest.mark.parametrize(
        "qgis_type,expected",
        [
            ("Point", "point"),
            ("MultiPoint", "point"),
            ("PointZ", "point"),
            ("PointZM", "point"),
            ("Point25D", "point"),
            ("LineString", "line"),
            ("MultiLineString", "line"),
            ("LineStringZ", "line"),
            ("Polygon", "polygon"),
            ("MultiPolygon", "polygon"),
            ("PolygonZ", "polygon"),
            # Lowercase variants should normalize the same.
            ("point", "point"),
            ("linestring", "line"),
            ("polygon", "polygon"),
        ],
    )
    def test_known_qgis_geometry_normalizes_to_v3(
        self, qgis_type: str, expected: str
    ) -> None:
        # Z / M / 25D dimensionality and Multi- prefixes must collapse
        # to the portal's three base types because the portal stores
        # everything as Multi-* under the hood and the v3 enum only
        # has point / line / polygon.
        assert qgis_geometry_to_v3(qgis_type) == expected

    def test_unknown_geometry_returns_none(self) -> None:
        # Unknown / non-spatial: we don't guess. The dialog uses None
        # to surface a warning and route to table-layer publishing.
        assert qgis_geometry_to_v3("NoGeometry") is None
        assert qgis_geometry_to_v3("") is None

    def test_geometrycollection_is_treated_as_unknown(self) -> None:
        # GeometryCollection has no single v3 mapping; refusing here
        # forces the user to split before publishing.
        assert qgis_geometry_to_v3("GeometryCollection") is None


class TestFieldTypeMapping:
    @pytest.mark.parametrize(
        "qgis_type,expected",
        [
            ("String", "string"),
            ("QString", "string"),
            ("Text", "string"),
            ("Int", "number"),
            ("Integer", "number"),
            ("LongLong", "number"),
            ("Double", "number"),
            ("Float", "number"),
            ("Real", "number"),
            ("Bool", "boolean"),
            ("Boolean", "boolean"),
            ("Date", "date"),
            ("DateTime", "date"),
            ("QDateTime", "date"),
        ],
    )
    def test_known_types_map_to_portal_vocab(
        self, qgis_type: str, expected: str
    ) -> None:
        assert qgis_field_type_to_v3(qgis_type) == expected

    def test_unknown_type_falls_back_to_string(self) -> None:
        # A hard refusal here would be unfriendly for obscure source
        # formats: the user can convert the column type after import.
        assert qgis_field_type_to_v3("Binary") == "string"
        assert qgis_field_type_to_v3("") == "string"
        assert qgis_field_type_to_v3("CustomType") == "string"


class TestSanitizeLayerId:
    def test_lowercases_and_strips_spaces(self) -> None:
        # The id ends up in URLs (OGC collection-id is
        # `<itemId>__<layerKey>`) so it has to be URL- and SQL-safe.
        assert sanitize_layer_id("My Parcels") == "my_parcels"

    def test_collapses_punctuation_to_single_underscore(self) -> None:
        # Multiple non-alnum runs would otherwise produce __ which
        # makes the collection-id parser ambiguous (the parser splits
        # on the literal `__`).
        assert sanitize_layer_id("a---b!!!c") == "a_b_c"

    def test_strips_leading_and_trailing_underscores(self) -> None:
        assert sanitize_layer_id("---roads---") == "roads"

    def test_prefixes_leading_digit(self) -> None:
        # SQL identifiers can't start with a digit; the engine names
        # per-scope tables with this so the prefix matters.
        assert sanitize_layer_id("2024_parcels") == "l_2024_parcels"

    def test_caps_at_forty_chars(self) -> None:
        long = "a" * 100
        assert sanitize_layer_id(long) == "a" * 40

    def test_empty_uses_fallback(self) -> None:
        assert sanitize_layer_id("") == "layer"
        assert sanitize_layer_id("###") == "layer"
        assert sanitize_layer_id("", fallback="custom") == "custom"


class TestLayerFromProbe:
    def test_probe_with_portal_normalized_geometry(self) -> None:
        # /ingest/stage returns the portal's vocab already
        # ('point' | 'line' | 'polygon'), not raw OGR strings.
        # We pass that through unchanged.
        probe = {
            "name": "Parcels",
            "geometryType": "polygon",
            "fields": [
                {"name": "PIN", "type": "string"},
                {"name": "acres", "type": "number"},
            ],
            "featureCount": 1389855,
        }
        layer = layer_from_probe(probe_layer=probe)
        assert layer.id == "parcels"
        assert layer.title == "Parcels"
        assert layer.geometry_type == "polygon"
        assert [f.name for f in layer.fields] == ["PIN", "acres"]
        assert layer.fields[0].type == "string"
        assert layer.fields[1].type == "number"

    def test_probe_with_raw_qgis_geometry_still_normalizes(self) -> None:
        # Defense in depth: if a future probe path leaks raw OGR
        # names like 'MultiPolygon', we still want the right answer.
        probe = {"name": "Roads", "geometryType": "MultiLineString", "fields": []}
        layer = layer_from_probe(probe_layer=probe)
        assert layer.geometry_type == "line"

    def test_probe_with_null_geometry_is_table_layer(self) -> None:
        probe: dict[str, Any] = {"name": "Lookup", "geometryType": None, "fields": []}
        layer = layer_from_probe(probe_layer=probe)
        assert layer.geometry_type is None

    def test_override_id_and_title(self) -> None:
        probe = {"name": "Parcels", "geometryType": "polygon", "fields": []}
        layer = layer_from_probe(
            probe_layer=probe, layer_id="wv_parcels", title="WV Parcels"
        )
        assert layer.id == "wv_parcels"
        assert layer.title == "WV Parcels"

    def test_empty_field_names_are_dropped(self) -> None:
        # Some OGR drivers emit nameless columns (e.g. a shapefile
        # with a corrupted header). The portal would reject them
        # at schema-create; drop here so the user sees real fields.
        probe = {
            "name": "X",
            "geometryType": "point",
            "fields": [
                {"name": "good", "type": "string"},
                {"name": "", "type": "string"},
                {"name": "  ", "type": "string"},
            ],
        }
        layer = layer_from_probe(probe_layer=probe)
        assert [f.name for f in layer.fields] == ["good"]

    def test_unknown_field_type_falls_back_to_string(self) -> None:
        probe = {
            "name": "X",
            "geometryType": "point",
            "fields": [{"name": "blob", "type": "Binary"}],
        }
        layer = layer_from_probe(probe_layer=probe)
        assert layer.fields[0].type == "string"


class TestDataLayerEnvelope:
    def test_single_layer_envelope_shape(self) -> None:
        # The portal's ItemsService.readV3Layers checks version === 3
        # before any further processing; pin that here.
        layer = V3Layer(
            id="parcels",
            title="Parcels",
            geometry_type="polygon",
            fields=[V3Field(name="PIN", type="string")],
        )
        env = build_data_layer_envelope(layers=[layer])
        assert env["version"] == 3
        assert isinstance(env["layers"], list)
        assert len(env["layers"]) == 1
        assert env["layers"][0]["id"] == "parcels"
        assert env["layers"][0]["geometryType"] == "polygon"

    def test_multi_layer_envelope_preserves_order(self) -> None:
        layers = [
            V3Layer(id="a", title="A", geometry_type="point"),
            V3Layer(id="b", title="B", geometry_type="line"),
            V3Layer(id="c", title="C", geometry_type="polygon"),
        ]
        env = build_data_layer_envelope(layers=layers)
        assert [lyr["id"] for lyr in env["layers"]] == ["a", "b", "c"]

    def test_field_dict_includes_label_default(self) -> None:
        # The portal uses field.label as the display name; we default
        # it to the field name so the user doesn't have to spell it
        # twice in the simple case.
        layer = V3Layer(
            id="x",
            title="X",
            geometry_type="point",
            fields=[V3Field(name="PIN", type="string")],
        )
        env = build_data_layer_envelope(layers=[layer])
        assert env["layers"][0]["fields"][0]["label"] == "PIN"

    def test_searchable_field_is_emitted_only_when_true(self) -> None:
        # Avoid emitting `searchable: false` because the portal's
        # engine indexing pass treats absent and false the same;
        # keeping the payload sparse helps diff debugging.
        layer = V3Layer(
            id="x",
            title="X",
            geometry_type="point",
            fields=[
                V3Field(name="a", type="string", searchable=True),
                V3Field(name="b", type="string", searchable=False),
            ],
        )
        env = build_data_layer_envelope(layers=[layer])
        assert env["layers"][0]["fields"][0]["searchable"] is True
        assert "searchable" not in env["layers"][0]["fields"][1]


def _summary(**overrides: Any) -> LayerSummary:
    defaults: dict[str, Any] = dict(
        name="Parcels",
        feature_count=100,
        geometry_type="Polygon",
        crs_auth_id="EPSG:4326",
        is_valid=True,
        field_names=["PIN", "acres"],
    )
    defaults.update(overrides)
    return LayerSummary(**defaults)


class TestValidateLayer:
    def test_clean_layer_passes(self) -> None:
        assert validate_layer(_summary()) == []

    def test_invalid_layer_is_an_error(self) -> None:
        issues = validate_layer(_summary(is_valid=False))
        assert any(i.is_error and i.code == "layer-invalid" for i in issues)

    def test_missing_crs_is_an_error(self) -> None:
        # We won't guess at a CRS: ingesting a layer with no SRS
        # would silently coordinate-mismatch with the basemap.
        issues = validate_layer(_summary(crs_auth_id=""))
        assert any(i.is_error and i.code == "missing-crs" for i in issues)

    def test_empty_layer_is_a_warning_only(self) -> None:
        # Empty is allowed: the user can grow the layer later through
        # the editor or via re-ingest.
        issues = validate_layer(_summary(feature_count=0))
        codes = {i.code: i.severity for i in issues}
        assert codes.get("empty-layer") == "warning"

    def test_non_spatial_geometry_is_a_warning_only(self) -> None:
        # The portal can take a table layer; warn so the user knows
        # it won't render on a map.
        issues = validate_layer(_summary(geometry_type="NoGeometry"))
        codes = {i.code: i.severity for i in issues}
        assert codes.get("non-spatial") == "warning"

    def test_duplicate_field_names_after_lowercase_are_an_error(self) -> None:
        # Postgres column names are case-folded; "Name" and "name"
        # would collide on import. Catch it before the wizard
        # commits to a ten-minute upload.
        issues = validate_layer(
            _summary(field_names=["Name", "NAME", "geom"])
        )
        assert any(i.is_error and i.code == "duplicate-field" for i in issues)

    def test_reserved_field_name_is_a_warning(self) -> None:
        # The portal will silently rename these; warn so the user
        # sees the rename coming.
        issues = validate_layer(
            _summary(field_names=["PIN", "created_at"])
        )
        assert any(
            i.severity == "warning" and i.code == "reserved-field" for i in issues
        )

    def test_multiple_issues_are_all_returned(self) -> None:
        # The dialog shows the full list (not first-error-only) so
        # the user can fix everything in one pass.
        issues = validate_layer(
            _summary(
                is_valid=False,
                crs_auth_id="",
                feature_count=0,
            )
        )
        codes = {i.code for i in issues}
        assert {"layer-invalid", "missing-crs", "empty-layer"} <= codes


class TestValidationIssue:
    def test_is_error_flag(self) -> None:
        assert ValidationIssue("error", "x", "m").is_error
        assert not ValidationIssue("warning", "x", "m").is_error
