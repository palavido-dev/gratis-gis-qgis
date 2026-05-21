# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edit flows (Phase 4).

  - sync.py: translate a QGIS edit buffer (added / changed-geom /
    changed-attrs / deleted) into a sequenced batch of portal
    feature-CRUD calls.

The UI ("Push edits to portal" action) lives in
`ui/push_edits_dialog.py` and calls these helpers.

Phase 4 covers QGIS-side editing of layers that originate from the
portal (OAPIF / vectortile sources we recognize via
`browser/uris.py`). The translation logic is pure-Python so the
tests don't need QGIS; the dialog wraps it with the QGIS edit-
buffer iteration helpers.
"""
