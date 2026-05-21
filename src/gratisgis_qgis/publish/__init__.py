# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish flows (Phase 3 + Phase 6).

  - project_to_map.py: translate the current QGIS project state
    into a portal `map` item composition (Phase 6).
  - vector.py: orchestrate a one-layer vector publish (Phase 3,
    landing next).

Each phase lands its UI in `ui/` and its pure-Python translation
helpers here so the shape-mapping rules can be unit-tested without
a QGIS runtime.
"""
