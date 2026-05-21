# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the raster publish recognizer + validator."""
from __future__ import annotations

import pytest

from gratisgis_qgis.publish.raster import (
    RasterValidationIssue,
    file_flavor,
    validate_raster_upload,
)


class TestFileFlavor:
    @pytest.mark.parametrize(
        "path,flavor",
        [
            ("parcels.pmtiles", "pmtiles"),
            ("/abs/path/parcels.PMTILES", "pmtiles"),  # case-insensitive
            ("tiles.mbtiles", "mbtiles"),
            ("dem.cog", "raster-cog-ready"),
            ("dem.tif", "raster-needs-convert"),
            ("dem.tiff", "raster-needs-convert"),
            ("dem.geotiff", "raster-needs-convert"),
            ("sat.jp2", "raster-needs-convert"),
        ],
    )
    def test_supported_extensions(self, path: str, flavor: str) -> None:
        # Pinning the recognizer against the portal's tile-layer
        # editor allow-list; adding a format here means landing the
        # matching change on the portal in lockstep.
        assert file_flavor(path).flavor == flavor

    def test_unknown_extension_is_unsupported_with_format_list(self) -> None:
        result = file_flavor("layer.shp")
        assert result.flavor == "unsupported"
        # The reason should tell the user what IS accepted so they
        # don't have to read source to find out.
        assert ".pmtiles" in result.reason
        assert ".tif" in result.reason

    def test_tpk_has_specific_reason(self) -> None:
        # TPK is a common ArcGIS export; users need to know it's
        # roadmap rather than thinking we'll never support it.
        result = file_flavor("cache.tpk")
        assert result.flavor == "unsupported"
        assert "roadmap" in result.reason.lower()

    def test_proprietary_codec_extensions_explain_the_license_block(self) -> None:
        # ECW / MrSID need vendor decoder libraries we can't ship
        # under AGPL. Spell that out so users know to convert
        # locally instead of waiting for support that won't come.
        for ext in ("scene.ecw", "image.sid"):
            result = file_flavor(ext)
            assert result.flavor == "unsupported"
            assert "AGPL" in result.reason or "decoder" in result.reason.lower()

    def test_no_extension_is_unsupported(self) -> None:
        assert file_flavor("filewithoutextension").flavor == "unsupported"


class TestRasterClassification:
    def test_is_tile_layer_true_for_pmtiles_and_raster(self) -> None:
        # All four supported flavors land as tile_layer items on
        # the portal; pinning this flags an accidental routing
        # split if someone adds a non-tile-layer flavor later.
        for path in ("a.pmtiles", "a.mbtiles", "a.cog", "a.tif"):
            assert file_flavor(path).is_tile_layer

    def test_is_tile_layer_false_for_unsupported(self) -> None:
        assert not file_flavor("a.shp").is_tile_layer

    def test_needs_server_conversion_only_for_mbtiles_and_raw_raster(self) -> None:
        assert not file_flavor("a.pmtiles").needs_server_conversion
        assert not file_flavor("a.cog").needs_server_conversion
        assert file_flavor("a.mbtiles").needs_server_conversion
        assert file_flavor("a.tif").needs_server_conversion


class TestValidateRasterUpload:
    def test_clean_pmtiles_passes(self) -> None:
        issues = validate_raster_upload(
            file_path="parcels.pmtiles",
            size_bytes=1024 * 1024,
            max_bytes=10 * 1024 * 1024 * 1024,
        )
        assert issues == []

    def test_unsupported_extension_is_an_error(self) -> None:
        issues = validate_raster_upload(
            file_path="parcels.shp",
            size_bytes=1024,
        )
        assert any(i.is_error and i.code == "unsupported-format" for i in issues)

    def test_zero_bytes_is_an_error(self) -> None:
        # Empty file would PUT successfully and then fail the
        # finalize header read; catch it before the upload.
        issues = validate_raster_upload(
            file_path="parcels.pmtiles",
            size_bytes=0,
        )
        assert any(i.is_error and i.code == "empty-file" for i in issues)

    def test_oversized_file_is_an_error(self) -> None:
        # The portal enforces maxBytes from the presign response;
        # catching it locally saves wasted upload time.
        issues = validate_raster_upload(
            file_path="parcels.pmtiles",
            size_bytes=20 * 1024 * 1024 * 1024,
            max_bytes=10 * 1024 * 1024 * 1024,
        )
        assert any(i.is_error and i.code == "exceeds-max" for i in issues)
        # Message should include both numbers so the user can size
        # the file appropriately.
        msg = next(i.message for i in issues if i.code == "exceeds-max")
        assert "GB" in msg and "MB" in msg

    def test_raw_raster_warns_about_conversion_time(self) -> None:
        # Server-side COG + PMTiles conversion for a county-scale
        # GeoTIFF is minutes, not seconds; warn so the user doesn't
        # assume the upload finished = the tile layer is viewable.
        issues = validate_raster_upload(
            file_path="dem.tif",
            size_bytes=1024 * 1024,
        )
        codes = {i.code: i.severity for i in issues}
        assert codes.get("needs-conversion") == "warning"

    def test_pmtiles_does_not_warn_about_conversion(self) -> None:
        # PMTiles is the final form; finalize is fast.
        issues = validate_raster_upload(
            file_path="parcels.pmtiles",
            size_bytes=1024,
        )
        assert all(i.code != "needs-conversion" for i in issues)

    def test_no_max_bytes_skips_the_size_check(self) -> None:
        # When the dialog hasn't yet called presign-upload it
        # doesn't know the max; validation should still run the
        # other checks.
        issues = validate_raster_upload(
            file_path="parcels.pmtiles",
            size_bytes=99 * 1024 * 1024 * 1024,
        )
        assert all(i.code != "exceeds-max" for i in issues)


class TestRasterValidationIssue:
    def test_is_error_flag(self) -> None:
        assert RasterValidationIssue("error", "x", "m").is_error
        assert not RasterValidationIssue("warning", "x", "m").is_error
