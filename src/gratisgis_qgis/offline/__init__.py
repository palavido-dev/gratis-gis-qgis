# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline clone flows (Phase 7).

  - clone.py: pure-Python helpers around picking a target file
    path for an offline GeoPackage clone, normalizing portal
    geojson into something GDAL writes cleanly, and pre-flight
    validation of the destination directory.

The UI ("Clone to GeoPackage" dialog) wraps these with the
QGIS-side QgsVectorFileWriter / project-load calls.
"""
