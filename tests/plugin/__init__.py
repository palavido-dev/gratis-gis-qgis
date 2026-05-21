# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin-side tests.

Plugin-side tests import only QGIS-agnostic helpers (anything that
requires `qgis.core` or `qgis.PyQt.*` stays untested at unit level
because the CI runner doesn't ship QGIS bindings). Module-by-
module split: keep the QGIS-touching code in items.py / provider.py
and the pure-Python rules (bucket filtering, URL building) in
their own modules so this suite can exercise them.
"""
