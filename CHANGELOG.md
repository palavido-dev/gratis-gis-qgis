# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project tracks [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for the client library; the QGIS plugin uses its own version line in
`metadata.txt` so that QGIS Plugin Repository semantics are honored.

## [0.1.0] - 2026-05-20

First end-to-end release: every phase of the original plan is in.
Pre-alpha, expect rough edges.

### Added

- **Phase 1 - Browse + add via OGC connection.** Browser-panel
  data provider exposes a `GratisGIS` subtree per configured
  connection with buckets (Mine, Shared, Org, Public) and per-item
  entries that drag onto the canvas as OAPIF or vectortile sources.
- **Phase 2 - Search + metadata.** Docked search panel for full-
  text portal search, per-item properties dialog with sharing /
  owner / extent / tags / description.
- **Phase 3 - Publish vector layer.** End-to-end dialog: pre-flight
  validation, GeoPackage export, staged upload, v3 data_layer item
  create, async import job with progress polling. Pure-Python
  translator owns the QGIS-to-v3 type mapping + layer-id
  sanitization rules.
- **Phase 4 - Push edits to portal.** Captures the QGIS edit
  buffer (adds, geometry changes, attribute changes, deletions)
  and translates it into ordered feature-CRUD calls. Co-occurring
  geom + attr edits to the same id merge into one PATCH;
  delete-after-update drops the update. Pure-Python planner.
- **Phase 5 - Publish raster / tile.** Upload PMTiles / MBTiles /
  GeoTIFF / COG / JP2 as a portal tile_layer. Pre-flight extension
  recognizer mirrors the portal's allow-list with format-specific
  reasons for TPK / ECW / MrSID. Direct PUT to MinIO via
  presigned URL, then portal-side finalize for header read +
  PMTiles pyramid build.
- **Phase 6 - Publish project as map.** Walks the QGIS project
  layer tree, recognizes layers backed by portal items, captures
  the canvas viewport (reprojected to CRS84), and creates a
  portal map item composing those layers. Skips layers from
  unknown providers with provider-aware reasons.
- **Phase 7 - Clone for offline.** Pulls the full feature set
  for a portal-backed OAPIF layer, normalizes portal-internal
  ids into a single `_portal_id` property (so the Phase 4
  push-edits round-trip stays lossless), and writes the result
  to a local GeoPackage that auto-loads into the project.
- **Phase 8 - Packaging.** `scripts/make_zip.py` builds a
  QGIS-installable plugin zip. The `gratisgis_client` library
  is vendored under `gratisgis_qgis/_vendor/` so the plugin
  works on a stock QGIS install with no extra pip steps;
  imports are rewritten on package.

### Foundation (previously unreleased)

- `gratisgis_client` portable Python client, `gratisgis_qgis`
  plugin shell, CI, tests, dev docs.
- PKCE OIDC auth flow against Keycloak.
- Connection management (add/edit/delete portal profiles).
- QGIS auth manager bridge for encrypted token storage.
- Plugin file logging.
- Keycloak realm dependency satisfied: a dedicated `qgis-plugin`
  public client ships in the GratisGIS realm (PKCE S256, custom
  scheme `gratisgis-qgis://auth-callback`, and loopback
  `http://127.0.0.1*` redirect URI). The plugin's default
  `client_id` resolves once the realm is imported.
- Portal discovery: `gratisgis_client.discover(portal_url)` fetches
  `/api/portal-info` and returns a typed `PortalInfo` (name, version,
  api base, OIDC issuer). `portal_config_from_discovery()` splits the
  issuer into the Keycloak realm pair the existing `PortalConfig`
  expects, so future endpoints can build clients from one URL.

### Changed

- **Connection dialog is one field.** The user enters only the
  portal URL; the plugin discovers everything else
  (display name, OIDC issuer, API base URL) automatically and
  caches it on the profile. The previous four-field form
  (Portal URL, Keycloak URL, Realm, Client ID) leaked Keycloak
  terminology into the user surface. Pre-existing four-field
  profiles continue to load: legacy `keycloak_url` + `realm` keys
  are synthesized into an `oidc_issuer` on read.

### Fixed

- Keycloak rejected the loopback redirect URI as `Invalid parameter:
  redirect_uri` because the registered pattern
  `http://127.0.0.1:*/*` placed a wildcard in the port position;
  Keycloak only supports trailing wildcards. The QGIS plugin client
  now registers `http://127.0.0.1*` which correctly matches any
  port and path. Applied to both the dev realm JSON and the prod
  template in the main gratis-gis repo.
- Plugin classFactory failed with `ModuleNotFoundError: No module
  named 'gratisgis_qgis'` when QGIS loaded the plugin under any
  folder name other than `gratisgis_qgis`. Internal imports inside
  `gratisgis_qgis` are now relative, so the QGIS plugin folder name
  can be anything (`gratisgis`, `gratisgis_qgis`, whatever the
  Plugin Repository or local install chooses). Smoke tested in
  QGIS 3.44 (OSGeo4W).
