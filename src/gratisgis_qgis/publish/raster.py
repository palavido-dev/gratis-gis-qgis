# SPDX-License-Identifier: AGPL-3.0-or-later
"""Raster publish planning (Phase 5).

The portal's raster path is a presigned-PUT upload of a single
file followed by a finalize call. Three flavors:

  - PMTiles (.pmtiles): a tile_layer item. The portal reads the
    PMTiles header on finalize, no further conversion needed.
  - MBTiles (.mbtiles): a tile_layer item. The portal converts
    to PMTiles server-side via the pyramid worker.
  - Raster (.tif / .tiff / .geotiff / .cog / .jp2): the portal
    runs COG conversion + PMTiles pyramid build server-side.

We don't try to convert raster -> PMTiles in the plugin: that
would require a CLI binary (tippecanoe / pmtiles) which we can't
reliably bundle in a QGIS plugin. The user converts externally
(or uploads a raw raster and lets the portal do the work).

This module owns the file recognition and pre-flight validation
rules. It's pure-Python so the publish dialog can stay thin and
the tests don't need QGIS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

# Match the portal's flavor classification on item.data.processingState.
RasterFlavor = Literal[
    "pmtiles",
    "mbtiles",
    "raster-cog-ready",
    "raster-needs-convert",
    "unsupported",
]


# File-extension classification. The portal accepts these per the
# tile-layer/editor.tsx allow-list; new extensions land in lockstep
# on both sides.
_PMTILES_EXTS = {".pmtiles"}
_MBTILES_EXTS = {".mbtiles"}
_COG_READY_EXTS = {".cog"}
"""Files already in cloud-optimized form; portal skips conversion."""

_RASTER_EXTS = {".tif", ".tiff", ".geotiff", ".jp2"}
"""Raw rasters the portal will COG-convert + pyramid server-side."""

_UNSUPPORTED_EXTS = {
    ".tpk": "TPK / TPKX support is on the roadmap. Convert to MBTiles or PMTiles first.",
    ".tpkx": "TPK / TPKX support is on the roadmap. Convert to MBTiles or PMTiles first.",
    ".ecw": "ECW ingest needs a proprietary decoder (AGPL-incompatible). Convert to GeoTIFF locally.",
    ".sid": "MrSID ingest needs a proprietary decoder (AGPL-incompatible). Convert to GeoTIFF locally.",
}


@dataclass(frozen=True)
class RasterClassification:
    """Outcome of file_flavor()."""

    flavor: RasterFlavor
    """High-level routing decision for the publish dialog."""

    reason: str = ""
    """Empty when flavor is one of the supported ones; populated
    with a user-actionable explanation when unsupported."""

    @property
    def is_tile_layer(self) -> bool:
        """True for .pmtiles / .mbtiles / .cog / raw raster -- the
        portal stores all of these as ``tile_layer`` items."""
        return self.flavor in ("pmtiles", "mbtiles", "raster-cog-ready", "raster-needs-convert")

    @property
    def needs_server_conversion(self) -> bool:
        return self.flavor in ("mbtiles", "raster-needs-convert")


def file_flavor(file_path: str) -> RasterClassification:
    """Classify a candidate upload file by extension.

    Extension-based because the portal does the same on its side
    (it inspects the magic bytes for sanity after upload, but the
    routing decision is by suffix). Doing it the same way here
    avoids two divergent recognizers.
    """
    ext = _ext(file_path)
    if ext in _PMTILES_EXTS:
        return RasterClassification(flavor="pmtiles")
    if ext in _MBTILES_EXTS:
        return RasterClassification(flavor="mbtiles")
    if ext in _COG_READY_EXTS:
        return RasterClassification(flavor="raster-cog-ready")
    if ext in _RASTER_EXTS:
        return RasterClassification(flavor="raster-needs-convert")
    if ext in _UNSUPPORTED_EXTS:
        return RasterClassification(
            flavor="unsupported", reason=_UNSUPPORTED_EXTS[ext]
        )
    return RasterClassification(
        flavor="unsupported",
        reason=(
            "Supported formats: .pmtiles, .mbtiles, .tif / .tiff / .geotiff, "
            ".cog, .jp2."
        ),
    )


def _ext(file_path: str) -> str:
    return os.path.splitext(file_path)[1].lower()


# -----------------------------------------------------------
# Pre-flight validation: size limit, file existence, etc.
# -----------------------------------------------------------


@dataclass(frozen=True)
class RasterValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


def validate_raster_upload(
    *,
    file_path: str,
    size_bytes: int,
    max_bytes: int | None = None,
) -> list[RasterValidationIssue]:
    """Run the pre-flight checks the publish dialog can do locally.

    The portal also runs its own checks (disk space, header sanity,
    bucket policy) on receive; this is the optimistic short-circuit
    so the user doesn't pay upload time for a doomed file.
    """
    issues: list[RasterValidationIssue] = []

    classification = file_flavor(file_path)
    if classification.flavor == "unsupported":
        issues.append(
            RasterValidationIssue(
                severity="error",
                code="unsupported-format",
                message=classification.reason or "Unsupported file format.",
            )
        )

    if size_bytes <= 0:
        issues.append(
            RasterValidationIssue(
                severity="error",
                code="empty-file",
                message="File is empty (0 bytes).",
            )
        )

    if max_bytes and size_bytes > max_bytes:
        gb = max_bytes / 1024 / 1024 / 1024
        actual_mb = size_bytes / 1024 / 1024
        issues.append(
            RasterValidationIssue(
                severity="error",
                code="exceeds-max",
                message=(
                    f"File is {actual_mb:.1f} MB but the portal's per-file "
                    f"limit is {gb:.1f} GB."
                ),
            )
        )

    if classification.needs_server_conversion:
        # Not blocking; the portal will do the work, but the wait
        # is significant for raw rasters. Surface so the user can
        # plan accordingly.
        issues.append(
            RasterValidationIssue(
                severity="warning",
                code="needs-conversion",
                message=(
                    "This format needs server-side conversion to PMTiles. "
                    "Expect a few minutes of post-upload processing before "
                    "the tile layer is viewable."
                ),
            )
        )

    return issues
