# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the offline-clone helpers (Phase 7)."""
from __future__ import annotations

import os

import pytest

from gratisgis_qgis.offline.clone import (
    CloneTarget,
    CloneValidationIssue,
    make_target,
    normalize_feature_collection,
    validate_clone_target,
)


class TestMakeTarget:
    def test_sanitizes_title_to_safe_filename(self) -> None:
        # Filenames have to survive a round-trip across Windows /
        # macOS / Linux. Strip punctuation, lowercase, collapse runs.
        t = make_target(
            directory="/tmp",
            item_title="My Parcels (West Virginia)!",
            layer_id="parcels",
        )
        assert t.file_name == "my_parcels_west_virginia"
        assert t.gpkg_path == os.path.join("/tmp", "my_parcels_west_virginia.gpkg")

    def test_falls_back_to_layer_id_when_title_is_empty(self) -> None:
        # An untitled portal item should still produce a writable
        # path; using the layer_id keeps it discoverable to the user.
        t = make_target(directory="/tmp", item_title="", layer_id="roads")
        assert "clone_roads" in t.file_name

    def test_falls_back_to_clone_layer_when_both_are_empty(self) -> None:
        t = make_target(directory="/tmp", item_title="", layer_id="")
        assert t.file_name == "clone_layer"

    def test_prefixes_leading_digit(self) -> None:
        # Filenames can start with digits but GeoPackage layer names
        # inferred from filenames inherit the SQL-identifier rule, so
        # we prefix to keep both safe.
        t = make_target(directory="/tmp", item_title="2024 Parcels", layer_id="x")
        assert t.file_name.startswith("clone_") or t.file_name[0].isalpha()

    def test_caps_filename_length(self) -> None:
        long_title = "a" * 200
        t = make_target(directory="/tmp", item_title=long_title, layer_id="x")
        # The stem alone shouldn't blow Windows's path budget for
        # the typical "C:\Users\<name>\Documents\GIS\" prefix.
        assert len(t.file_name) <= 80


class TestNormalizeFeatureCollection:
    def test_passes_clean_collection_through(self) -> None:
        body = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                    "properties": {"name": "X"},
                }
            ],
        }
        out = normalize_feature_collection(body)
        assert out["type"] == "FeatureCollection"
        assert len(out["features"]) == 1
        assert out["features"][0]["properties"]["name"] == "X"

    def test_non_dict_body_yields_empty_collection(self) -> None:
        # An upstream proxy injecting a string body would otherwise
        # crash the GeoPackage writer on .get(); shielding here keeps
        # the dialog from collapsing on a single network hiccup.
        assert normalize_feature_collection("nope") == {
            "type": "FeatureCollection",
            "features": [],
        }
        assert normalize_feature_collection(None) == {
            "type": "FeatureCollection",
            "features": [],
        }

    def test_missing_features_key_yields_empty_collection(self) -> None:
        out = normalize_feature_collection({"type": "FeatureCollection"})
        assert out["features"] == []

    def test_malformed_feature_is_dropped(self) -> None:
        # One bad row should NOT take down the whole clone; drop
        # the bad ones and let the user see N-1 features locally.
        body = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": None, "properties": {"a": 1}},
                "not-a-feature",
                {"type": "NotFeature", "geometry": None},
                {"type": "Feature", "geometry": None, "properties": {"a": 2}},
            ],
        }
        out = normalize_feature_collection(body)
        assert len(out["features"]) == 2
        assert [f["properties"]["a"] for f in out["features"]] == [1, 2]


class TestPortalIdNormalization:
    def test_feature_id_moves_into_portal_id_property(self) -> None:
        # The portal's per-feature id collides with QGIS's auto-
        # assigned fid when written to GeoPackage. Move it under
        # _portal_id so the round-trip stays lossless and the
        # push-edits flow can still find the portal id later.
        body = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "abc-123",
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                    "properties": {"name": "X"},
                }
            ],
        }
        out = normalize_feature_collection(body)
        props = out["features"][0]["properties"]
        assert props["_portal_id"] == "abc-123"
        assert props["name"] == "X"

    def test_id_in_properties_also_gets_moved(self) -> None:
        body = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": None,
                    "properties": {"id": "p1", "name": "X"},
                }
            ],
        }
        out = normalize_feature_collection(body)
        props = out["features"][0]["properties"]
        assert props["_portal_id"] == "p1"
        # Source alias is removed so a re-clone doesn't double-write.
        assert "id" not in props
        assert props["name"] == "X"

    def test_feature_without_id_has_no_portal_id(self) -> None:
        body = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": None,
                    "properties": {"name": "X"},
                }
            ],
        }
        out = normalize_feature_collection(body)
        assert "_portal_id" not in out["features"][0]["properties"]

    @pytest.mark.parametrize("alias", ["fid", "feature_id", "featureId"])
    def test_alias_columns_become_portal_id(self, alias: str) -> None:
        body = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": None,
                    "properties": {alias: "x"},
                }
            ],
        }
        out = normalize_feature_collection(body)
        props = out["features"][0]["properties"]
        assert props["_portal_id"] == "x"


class TestValidateCloneTarget:
    def test_existing_writable_directory_passes(self, tmp_path) -> None:
        target = CloneTarget(directory=str(tmp_path), file_name="x")
        issues = validate_clone_target(target)
        # tmp_path is writable; no errors. No warnings either because
        # the file doesn't exist yet.
        assert issues == []

    def test_missing_directory_is_an_error(self) -> None:
        target = CloneTarget(directory="/nonexistent/path/xyz", file_name="x")
        issues = validate_clone_target(target)
        assert any(i.is_error and i.code == "directory-missing" for i in issues)

    def test_empty_directory_is_an_error(self) -> None:
        target = CloneTarget(directory="", file_name="x")
        issues = validate_clone_target(target)
        assert any(i.is_error and i.code == "no-directory" for i in issues)

    def test_existing_file_warns_about_overwrite(self, tmp_path) -> None:
        # Pre-create the target file; the dialog can decide whether
        # to ask for confirmation based on this warning.
        (tmp_path / "x.gpkg").write_text("")
        target = CloneTarget(directory=str(tmp_path), file_name="x")
        issues = validate_clone_target(target)
        warns = [i for i in issues if i.code == "target-exists"]
        assert len(warns) == 1
        assert warns[0].severity == "warning"


class TestCloneValidationIssue:
    def test_is_error_flag(self) -> None:
        assert CloneValidationIssue("error", "x", "m").is_error
        assert not CloneValidationIssue("warning", "x", "m").is_error
