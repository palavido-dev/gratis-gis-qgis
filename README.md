# GratisGIS for QGIS

The official QGIS plugin for [GratisGIS](https://gratisgis.org), the
open source self-hosted geospatial portal. It makes a portal feel
native inside QGIS: browse and search portal content, add layers
with working attribute tables, open portal maps with their saved
styling, publish vector and raster data, push edits back, share
items, and clone layers for offline work.

**Status:** Stable, v1.0.2. Submitted to the QGIS Plugin Repository
(approval in progress). Requires a GratisGIS portal running 0.9.29
or newer.

**License:** AGPL-3.0-or-later. Same as GratisGIS itself.

**Target QGIS version:** 3.34 LTS or newer, including QGIS 4 / Qt 6.

## Install

The plugin is in the official
[QGIS plugin repository](https://plugins.qgis.org/plugins/gratisgis_qgis/):
in QGIS, open Plugins > Manage and Install Plugins, search for
**GratisGIS**, and install.

To install a specific build instead, download the zip from the
[releases page](https://github.com/palavido-dev/gratis-gis-qgis/releases)
and use Install from ZIP, or build it yourself from a checkout:

```
python scripts/make_zip.py
```

The zip is self-contained. The portal client library is vendored
inside it and uses only the Python standard library, so a stock QGIS
install needs no pip steps.

## What works today

- Browser panel tree per portal connection, with My Content,
  Shared with Me, Org Content, and Public buckets, plus a search
  dock and a per-item properties dialog.
- Layers add as real feature layers with working attribute tables
  by default; very large layers render as fast vector tiles, with
  the feature version one right-click away (and the reverse).
- Open a portal map in QGIS with its saved layers, symbology,
  visibility, groups, and viewport, from the Browser or the
  toolbar's map picker.
- Publish a vector layer, several layers as one item, or the whole
  QGIS project as a portal map; published maps keep QGIS symbology.
  Drag a local layer onto My Content to publish it.
- Publish PMTiles / GeoTIFF / MBTiles as a portal tile_layer.
- Push QGIS edits (adds, deletes, geometry and attribute changes)
  back to portal layers, and clone layers to a local GeoPackage for
  offline work, from dialogs, the Layers panel context menu, or as
  Processing algorithms (batchable in the model designer).
- Share items from QGIS: private, organization, public, or specific
  groups.
- Everything slow runs as a background task with progress and
  cancel; the UI stays responsive.

## Private layers

Signing in mints a read-only portal API key, stores it in the QGIS
authentication database, and attaches it to non-public layer URIs so
private and org-shared content works everywhere: spatial layers,
tables, rasters, and the signed-in feature surface that powers
attribute tables. The key is revoked at sign-out. Edits always go
through your signed-in OAuth session, never the key, so a leaked
layer URI cannot modify data.

## Authentication

Connecting is one field: the portal URL. The plugin fetches
`<portal>/api/portal-info` to discover the portal's display name,
API base URL, and OIDC issuer, then signs in with OAuth + PKCE
against the portal's Keycloak realm. Tokens are stored in the QGIS
auth manager (encrypted, the same store QGIS uses for database and
WFS credentials) and refresh automatically.

The plugin sends `client_id=qgis-plugin` on every PKCE handshake.
That client ships in the realm export under `infra/keycloak/` in the
main gratis-gis repo, with the loopback prefix `http://127.0.0.1*`
registered as a valid redirect URI. Re-import the realm after
pulling the latest GratisGIS so a fresh deployment picks it up.

## Repo layout

```
src/
  gratisgis_client/     Portal API client. Synchronous, pure
                        standard library, no QGIS imports. Vendored
                        into the plugin zip at build time.
  gratisgis_qgis/       The QGIS plugin: Browser provider, dialogs,
                        background tasks, auth manager bridge.
tests/
  client/               Client tests (no QGIS needed).
  plugin/               Plugin tests against a stubbed qgis module.
  packaging/            Builds the zip and imports every module of
                        it in a clean interpreter.
scripts/
  make_zip.py           Builds dist/gratisgis_qgis-<version>.zip.
  qgis_smoke.py         Checks the plugin against a real QGIS
                        install (see Development).
docs/
  development.md        Dev environment and loading the plugin into
                        QGIS without packaging it.
```

## Development

There are no runtime dependencies; the dev tools are the whole
setup:

```
pip install pytest ruff "mypy>=1.10,<2.0"
python -m pytest tests/ -q
python -m ruff check src tests
python -m mypy
```

Those tests run against a simulated QGIS, so they are fast and need
nothing installed, but a simulated QGIS accepts anything: it cannot
catch a value of the wrong type being handed to a real QGIS function.
That gap is real, and it shipped a crash once. So there is a second
check that runs against an actual QGIS install, using QGIS's own
Python rather than your virtual environment:

```
# Windows / OSGeo4W
C:\OSGeo4W\bin\python-qgis.bat scripts\qgis_smoke.py

# Linux / macOS, where python3 already has the qgis bindings
python3 scripts/qgis_smoke.py
```

It needs no portal and no network, prints a line per check, and exits
non-zero if anything fails. Run it before releasing, and after any
change that touches QGIS APIs, enums, or background tasks. It is not
part of CI because the CI runners have no QGIS.

To run the plugin inside a live QGIS during development, see
[`docs/development.md`](docs/development.md).

## Contributing

This is a solo project today. Issues and PRs welcome.
