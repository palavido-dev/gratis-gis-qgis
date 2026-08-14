# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the offline-clone helpers (Phase 7)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from gratisgis_qgis.browser.uris import PortalLayerRef
from gratisgis_qgis.offline.clone import (
    CLONE_SOURCE_FIELDS,
    CLONE_SOURCE_TABLE,
    CloneTarget,
    CloneValidationIssue,
    clone_timestamp,
    make_target,
    normalize_feature_collection,
    read_clone_source,
    safe_write_path,
    validate_clone_target,
)


class TestSafeWritePath:
    def test_success_promotes_temp_into_place(self, tmp_path: Path) -> None:
        final = tmp_path / "clone.gpkg"
        with safe_write_path(str(final)) as tmp:
            Path(tmp).write_bytes(b"new bytes")
        assert final.read_bytes() == b"new bytes"
        # No .part leftovers once promoted.
        assert list(tmp_path.glob("*.part")) == []

    def test_temp_lives_beside_the_target(self, tmp_path: Path) -> None:
        # Staging inside a temp DIRECTORY in the target's folder, not a
        # temp FILE beside it: same filesystem either way (so the final
        # replace is an atomic rename), but the yielded path does not
        # exist yet and keeps the real extension, both of which OGR
        # needs to create a GeoPackage there at all.
        final = tmp_path / "clone.gpkg"
        with safe_write_path(str(final)) as tmp:
            assert Path(tmp).name == "clone.gpkg"
            assert not Path(tmp).exists()
            assert Path(tmp).parent.parent == tmp_path
            Path(tmp).write_bytes(b"x")
        assert final.read_bytes() == b"x"
        # No staging directory left behind.
        assert list(tmp_path.iterdir()) == [final]

    def test_failure_preserves_existing_target(self, tmp_path: Path) -> None:
        # The defect this exists for: the old flow unlinked the
        # user's previous clone BEFORE writing, so a failed write
        # destroyed it. Now the target must survive untouched.
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"previous clone, possibly edited")
        with (
            pytest.raises(RuntimeError, match="writer exploded"),
            safe_write_path(str(final)) as tmp,
        ):
            Path(tmp).write_bytes(b"partial garbage")
            raise RuntimeError("writer exploded")
        assert final.read_bytes() == b"previous clone, possibly edited"
        # And the partial temp file is cleaned up.
        assert list(tmp_path.glob("*.part")) == []

    def test_failure_with_no_existing_target_leaves_nothing(
        self, tmp_path: Path
    ) -> None:
        final = tmp_path / "clone.gpkg"
        with pytest.raises(RuntimeError), safe_write_path(str(final)):
            raise RuntimeError("boom")
        assert not final.exists()
        assert list(tmp_path.iterdir()) == []


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
    def test_existing_writable_directory_passes(self, tmp_path: Path) -> None:
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

    def test_existing_file_warns_about_overwrite(self, tmp_path: Path) -> None:
        # Pre-create the target file; the dialog can decide whether
        # to ask for confirmation based on this warning.
        (tmp_path / "x.gpkg").write_text("")
        target = CloneTarget(directory=str(tmp_path), file_name="x")
        issues = validate_clone_target(target)
        warns = [i for i in issues if i.code == "target-exists"]
        assert len(warns) == 1
        assert warns[0].severity == "warning"


def _write_source_row(path: Path, values: tuple[str, str, str, str]) -> None:
    """Put a clone-origin row into a GeoPackage-shaped SQLite file.

    A GeoPackage is a SQLite database and the origin table is a plain
    attribute table, so this is the same storage the QGIS writer
    produces. Column names come from the shared constant, so a rename
    on the writing side cannot leave this passing against a stale one.
    """
    conn = sqlite3.connect(str(path))
    try:
        columns = ", ".join(f'"{name}" TEXT' for name in CLONE_SOURCE_FIELDS)
        placeholders = ", ".join("?" for _ in CLONE_SOURCE_FIELDS)
        conn.execute(f'CREATE TABLE "{CLONE_SOURCE_TABLE}" ({columns})')
        conn.execute(
            f'INSERT INTO "{CLONE_SOURCE_TABLE}" VALUES ({placeholders})', values
        )
        conn.commit()
    finally:
        conn.close()


class TestReadCloneSource:
    """Recovering which portal layer a clone came from.

    Without this the push-edits dialog can never offer an offline
    clone back to its origin, which is the whole point of writing the
    origin into the container in the first place.
    """

    def test_round_trips_the_recorded_origin(self, tmp_path: Path) -> None:
        gpkg = tmp_path / "trails.gpkg"
        _write_source_row(
            gpkg,
            ("https://portal.example", "item-1", "trails", clone_timestamp()),
        )
        assert read_clone_source(str(gpkg)) == PortalLayerRef(
            portal_url="https://portal.example", item_id="item-1", layer_id="trails"
        )

    def test_geopackage_without_the_table_is_not_a_clone(
        self, tmp_path: Path
    ) -> None:
        # The common case: an unrelated GeoPackage in the user's
        # project. Quiet and negative, never an exception.
        gpkg = tmp_path / "survey.gpkg"
        sqlite3.connect(str(gpkg)).close()
        assert read_clone_source(str(gpkg)) is None

    def test_missing_file_returns_none_and_creates_nothing(
        self, tmp_path: Path
    ) -> None:
        # Read-only open matters here: a probe must never leave an
        # empty database behind at a path the user never had.
        missing = tmp_path / "nope.gpkg"
        assert read_clone_source(str(missing)) is None
        assert not missing.exists()

    def test_non_sqlite_file_returns_none(self, tmp_path: Path) -> None:
        junk = tmp_path / "notes.gpkg"
        junk.write_text("this is not a database")
        assert read_clone_source(str(junk)) is None

    def test_empty_path_returns_none(self) -> None:
        assert read_clone_source("") is None

    def test_blank_ids_are_not_a_usable_origin(self, tmp_path: Path) -> None:
        # A half-written row would otherwise resolve to a push aimed at
        # an empty item id, which the portal answers with a 404 the
        # user cannot interpret.
        gpkg = tmp_path / "broken.gpkg"
        _write_source_row(gpkg, ("https://portal.example", "item-1", "", "now"))
        assert read_clone_source(str(gpkg)) is None


class TestCloneTimestamp:
    def test_is_iso_8601_utc(self) -> None:
        from datetime import datetime, timezone

        stamp = clone_timestamp()
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timezone.utc.utcoffset(None)


class TestCloneValidationIssue:
    def test_is_error_flag(self) -> None:
        assert CloneValidationIssue("error", "x", "m").is_error
        assert not CloneValidationIssue("warning", "x", "m").is_error
