# GratisGIS for QGIS

A QGIS plugin that makes GratisGIS portals feel native inside QGIS:
browse portal content from the Browser panel, search the portal,
drag layers and maps to the canvas, publish vector and raster data,
push edits back, and clone layers for offline work.

**Status:** Pre-alpha, v0.2.0. Works against GratisGIS v0.9.x
portals. Not yet listed in the QGIS Plugin Repository.

**License:** AGPL-3.0-or-later. Same as GratisGIS itself.

**Target QGIS version:** 3.34 LTS or newer, including QGIS 4 / Qt 6.

## Install

Download the plugin zip from the
[releases page](https://github.com/palavido-dev/gratis-gis-qgis/releases),
then in QGIS: Plugins > Manage and Install Plugins > Install from ZIP.
Or build the zip yourself from a checkout:

```
python scripts/make_zip.py
```

The zip is self-contained. The portal client library is vendored
inside it and uses only the Python standard library, so a stock QGIS
install needs no pip steps.

## What works today

- Browser panel tree per portal connection, with My Content,
  Shared with Me, Org Content, and Public buckets.
- Search dock for full-text portal search, plus a per-item
  properties dialog.
- Drag data layers, maps, basemaps, tile layers, and connected
  services onto the canvas.
- Publish a vector layer as a portal data_layer (GeoPackage export,
  staged upload, import job with progress).
- Publish PMTiles / GeoTIFF / MBTiles as a portal tile_layer.
- Publish the current QGIS project as a portal map.
- Push QGIS edits (adds, deletes, geometry and attribute changes)
  back to portal layers.
- Clone a portal layer to a local GeoPackage for offline use.
- Sign-in, publishes, clone, and push all run as background tasks
  with progress and cancel; the UI stays responsive.

## Private layers

Signing in mints a read-only portal API key, stores it in the QGIS
authentication database, and attaches it to non-public layer URIs so
private and org-shared layers render on the canvas; the key is
revoked at sign-out. Edits always go through your signed-in OAuth
session, never the key, so a leaked layer URI cannot modify data.

Known limits: private non-spatial tables are clone-to-edit, layers
added from the search dock use the public surface, and tile_layer
items are served from the public surface only.

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

To run the plugin inside a live QGIS during development, see
[`docs/development.md`](docs/development.md).

## Contributing

This is a solo project today. Issues and PRs welcome.
