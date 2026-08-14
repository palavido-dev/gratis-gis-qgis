# SPDX-License-Identifier: AGPL-3.0-or-later
"""Work out what a project layer can be published from.

Publishing a raster used to mean finding the file on disk yourself,
even when the thing you wanted to publish was already drawn on your
map. The natural thought is "this aerial is in my map, put it on the
portal", so the picker offers project layers and works the file out.

Only the resolving lives here, and it is pure text and filesystem
checks so it can be tested without QGIS. Deciding what to do with the
answer is the dialog's job.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

#: Prefixes that mean the raster is streamed rather than stored: a web
#: service, or a file read over HTTP. There is no local file to upload,
#: and reaching through to fetch one would be a surprising amount of
#: work to do on someone's behalf.
_REMOTE_MARKERS = ("/vsicurl/", "/vsis3/", "/vsigs/", "/vsiaz/", "/vsizip//vsicurl/")


@dataclass(frozen=True)
class RasterSource:
    """Where a raster layer's pixels actually live."""

    file_path: str = ""
    reason: str = ""
    """Plain wording for why it cannot be published, when it cannot."""

    @property
    def is_publishable(self) -> bool:
        return bool(self.file_path)


def resolve_raster_source(source: str, provider: str = "") -> RasterSource:
    """Find the file behind a raster layer, or say why there is none.

    ``source`` is the layer's data source string and ``provider`` its
    provider name when the caller knows it (``gdal`` for file-backed
    rasters, ``wms`` for tiled services including XYZ).

    Deliberately strict: it returns a path only when that path is a
    file that exists right now. A raster the user cannot see the whole
    of, or one assembled from several files, would otherwise upload
    something other than what they were looking at.
    """
    if not source:
        return RasterSource(reason="This layer has no file behind it.")

    text = source.strip()

    # A tiled web service: XYZ, WMS, WMTS. These arrive as a parameter
    # string rather than a path.
    if provider.lower() in {"wms", "wmts", "xyz"} or text.startswith("type="):
        return RasterSource(
            reason=(
                "This layer streams from a web service, so there is no file "
                "to publish. Export it first (right-click the layer > Export "
                "> Save As, choosing GeoTIFF), then publish that file."
            )
        )

    for marker in _REMOTE_MARKERS:
        if text.startswith(marker) or marker in text:
            return RasterSource(
                reason=(
                    "This layer is read straight from the internet, so there "
                    "is no file on this computer to publish. That includes "
                    "layers already coming from a GratisGIS portal."
                )
            )

    # Subdataset syntax, e.g. NETCDF:"file.nc":temperature. The
    # container holds more than the one band on screen, so uploading it
    # would publish something other than what the user picked.
    if _looks_like_subdataset(text):
        return RasterSource(
            reason=(
                "This layer is one band inside a larger file, so publishing "
                "the file would include more than what you see. Export just "
                "this layer first, then publish that."
            )
        )

    candidate = text.split("|", 1)[0].strip()
    if not candidate:
        return RasterSource(reason="This layer has no file behind it.")
    if os.path.isfile(candidate):
        return RasterSource(file_path=candidate)

    return RasterSource(
        reason=(
            "The file behind this layer could not be found on this "
            f"computer: {candidate}"
        )
    )


def _looks_like_subdataset(text: str) -> bool:
    """Whether the source names a piece inside a container file.

    The GDAL spelling is ``DRIVER:"path":component``, sometimes without
    the quotes. Checked before the plain-path branch because on Windows
    a bare path also contains a colon (``C:\\...``), so an unguarded
    colon test would reject every ordinary file.
    """
    head, sep, rest = text.partition(":")
    if not sep or not rest:
        return False
    # A Windows drive letter is a single character; a driver name is
    # several, and upper case by convention.
    if len(head) <= 1:
        return False
    if not head.isupper() or not head.replace("_", "").isalnum():
        return False
    # A driver prefix is always followed by another colon separating the
    # component name, once the quoted path is accounted for.
    return ":" in rest


#: Extensions the portal's tile-layer ingest accepts. A raster in the
#: project stored as something else can still be published, but only
#: after being exported, so the picker says so rather than letting the
#: upload fail at the far end.
PUBLISHABLE_RASTER_SUFFIXES = (
    ".tif",
    ".tiff",
    ".geotiff",
    ".cog",
    ".jp2",
    ".pmtiles",
    ".mbtiles",
)


def raster_suffix_is_supported(file_path: str) -> bool:
    return file_path.lower().endswith(PUBLISHABLE_RASTER_SUFFIXES)


@dataclass(frozen=True)
class PublishChoice:
    """One entry in the publish picker."""

    kind: Literal["vector", "raster", "file"]
    label: str
    layer_id: str = ""
    file_path: str = ""
    reason: str = ""

    @property
    def is_publishable(self) -> bool:
        return not self.reason
