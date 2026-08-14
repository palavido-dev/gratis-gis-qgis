# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finding the file behind a raster layer already on the canvas.

Publishing a raster used to mean hunting for the file yourself, even
when the thing you wanted to publish was drawn on your map. These cover
the resolving: which layers can be published from the project, and what
a user is told about the ones that cannot.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gratisgis_qgis.publish.source import resolve_raster_source


class TestFileBackedRasters:
    def test_a_plain_file_resolves_to_itself(self, tmp_path: Path) -> None:
        tif = tmp_path / "aerial.tif"
        tif.write_bytes(b"not really a tiff, but it exists")
        assert resolve_raster_source(str(tif), "gdal").file_path == str(tif)

    def test_pipe_options_are_stripped(self, tmp_path: Path) -> None:
        tif = tmp_path / "aerial.tif"
        tif.write_bytes(b"x")
        resolved = resolve_raster_source(f"{tif}|band=1", "gdal")
        assert resolved.file_path == str(tif)

    def test_a_windows_path_is_not_mistaken_for_a_subdataset(
        self, tmp_path: Path
    ) -> None:
        # A drive letter puts a colon in every Windows path, and the
        # subdataset check keys on colons, so this is the case that
        # would break every raster on Matt's own machine.
        tif = tmp_path / "aerial.tif"
        tif.write_bytes(b"x")
        resolved = resolve_raster_source(str(tif), "gdal")
        assert resolved.is_publishable, resolved.reason


class TestRastersThatCannotBePublished:
    """Each one has to say WHY, and say it without jargon."""

    def _reason(self, source: str, provider: str = "") -> str:
        resolved = resolve_raster_source(source, provider)
        assert not resolved.is_publishable
        assert resolved.reason
        return resolved.reason

    @pytest.mark.parametrize("provider", ["wms", "WMS", "wmts", "xyz"])
    def test_a_web_service_explains_itself(self, provider: str) -> None:
        reason = self._reason(
            "type=xyz&url=https://tile.example/{z}/{x}/{y}.png", provider
        )
        assert "web service" in reason.lower()
        assert "export" in reason.lower()

    def test_a_service_source_is_caught_even_without_a_provider_name(self) -> None:
        # The provider is not always available on a stand-in layer.
        assert self._reason("type=xyz&url=https://tile.example/{z}/{x}/{y}.png")

    def test_a_layer_read_over_the_internet_explains_itself(self) -> None:
        reason = self._reason("/vsicurl/https://portal.example/file.cog", "gdal")
        assert "internet" in reason.lower()

    def test_a_portal_raster_is_covered_by_that_same_wording(self) -> None:
        # Layers added from the GratisGIS tree are exactly this shape,
        # and "publish the thing you are already looking at" is a very
        # natural mistake to make with one.
        reason = self._reason(
            "/vsicurl/https://gratisgis.org/api/tile-layer/abc/file.cog", "gdal"
        )
        assert "gratisgis" in reason.lower()

    def test_a_band_inside_a_container_explains_itself(self) -> None:
        reason = self._reason('NETCDF:"/data/climate.nc":temperature', "gdal")
        assert "band" in reason.lower()

    def test_a_missing_file_names_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.tif"
        assert str(missing) in self._reason(str(missing), "gdal")

    def test_an_empty_source_is_refused_rather_than_crashing(self) -> None:
        assert self._reason("")

    def test_a_directory_is_not_a_file(self, tmp_path: Path) -> None:
        assert self._reason(str(tmp_path), "gdal")


class TestReasonsAvoidJargon:
    """Matt's standing rule: the UI speaks plain English.

    These strings are shown to users, so they must not leak the names
    of the libraries involved.
    """

    @pytest.mark.parametrize(
        "source,provider",
        [
            ("type=xyz&url=https://tile.example/{z}/{x}/{y}.png", "wms"),
            ("/vsicurl/https://portal.example/file.cog", "gdal"),
            ('NETCDF:"/data/climate.nc":temperature', "gdal"),
        ],
    )
    def test_no_library_names_leak_into_the_wording(
        self, source: str, provider: str
    ) -> None:
        reason = resolve_raster_source(source, provider).reason.lower()
        for jargon in ("gdal", "vsicurl", "provider", "uri", "wms", "netcdf"):
            assert jargon not in reason, f"{jargon!r} leaked into: {reason}"
