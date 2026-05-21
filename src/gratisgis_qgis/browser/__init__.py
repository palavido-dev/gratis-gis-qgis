# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browser panel integration for the GratisGIS plugin (Phase 1).

QGIS exposes its Browser panel through a `QgsDataItemProvider`
subclass registered with `QgsApplication.dataItemProviderRegistry`.
The provider returns a root `QgsDataCollectionItem` which lazily
expands into per-connection / per-item children.

Module layout:
    provider.py - the QgsDataItemProvider that QGIS registers
    items.py    - the QgsDataCollectionItem / QgsLayerItem subclasses
                  that compose the Browser tree
    fetch.py    - thread-safe fetch helpers that talk to the
                  portal-api over the existing gratisgis_client

The Browser panel is a synchronous tree but our portal access is
async; the fetch layer wraps each call in `qgis_async.run_async`
(see fetch.py) so a tree expand doesn't block the UI thread.
"""
