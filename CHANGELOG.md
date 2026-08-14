# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project tracks [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for the client library; the QGIS plugin uses its own version line in
`metadata.txt` so that QGIS Plugin Repository semantics are honored.

## [0.2.3] - 2026-08-14

### Fixed

- **Tile layers added an empty layer instead of the imagery.** Tile
  layers hold raster data (elevation, hillshade, imagery), but the
  plugin was asking the portal for them as vector tiles, an address
  that does not exist for these items, so QGIS added a layer that drew
  nothing and reported no error. Tile layers stored as Cloud Optimized
  GeoTIFF now open directly over the network, reading only the parts of
  the image needed for the current view, and private and organization
  layers carry their credential so they draw for anyone signed in.

### Changed

- **Tile layers QGIS cannot open now say so instead of failing
  quietly.** Layers stored as PMTiles cannot be opened by QGIS at all
  (its raster reader does not support that container), and layers still
  being prepared on the portal are not ready yet. Both now appear in
  the browser with an explanation rather than as a layer that silently
  draws nothing. Use the Download button on the item's portal page to
  work with a PMTiles layer in QGIS for now.

## [0.2.2] - 2026-08-13

### Fixed

- **The plugin failed to load with `KeyError: 'gratisgis_qgis._vendor'`
  when QGIS reloaded it**, which is what happens when you install over
  an existing copy or toggle the plugin off and on. The first load in a
  fresh QGIS session worked, so this only appeared on the second.
  QGIS clears only the modules its own import hook recorded, and the
  bundled client library is registered by the plugin directly, so a
  stale entry survived the unload and sent the next load down a path
  that skipped part of the setup. The plugin now clears both halves of
  its own state on every load and no longer assumes the surviving
  entry is present. The packaging test now performs a second load the
  same way QGIS does, so this cannot regress unnoticed.

## [0.2.1] - 2026-08-13

### Fixed

- **Sign-in crashed immediately on QGIS 4.** Starting any background
  task raised `TypeError: QgsTask(): argument 2 has unexpected type
  'int'`, so connecting to a portal failed at the first step. The
  cause was a compatibility branch that decided which value to pass
  by asking whether the flag was an integer; on Qt6 the flag types
  are integers as well, so the branch fired on exactly the QGIS
  version it was written to support. QGIS 3 is unaffected.

### Added

- `scripts/qgis_smoke.py`, a headless check that runs against a real
  QGIS install (`python-qgis.bat scripts/qgis_smoke.py`). The unit
  tests run against a simulated QGIS, which cannot catch a value of
  the wrong type being handed to a real QGIS function, which is how
  the crash above shipped. The new check exercises the plugin against
  the genuine bindings: every module imports, every version-dependent
  enum resolves, a background task runs start to finish, the browser
  provider builds, the layer addresses parse, and the authentication
  method that private layers rely on is present.

## [0.2.0] - 2026-08-13

A foundation release: same features as 0.1.0, rebuilt so they
actually hold up in a real QGIS. Still pre-alpha.

### Fixed

- **The plugin zip now installs and runs on stock QGIS.** Previous
  zips, including the 0.1.0 release, did not: the plugin required
  Python packages (httpx, pydantic) that QGIS does not ship and the
  zip could not carry, and the build's import rewriting left the
  vendored client library unable to import itself. The client is
  now pure Python standard library with no third-party dependencies
  at all, the zip vendors it unmodified, and a packaging test
  builds the zip and imports every module of it on a bare
  interpreter so a dead-on-arrival zip cannot ship again.
- **The UI no longer freezes during network work.** Sign-in,
  vector / raster / project publishes, offline clone, and push
  edits all run as background tasks with progress reporting and a
  working Cancel. Previously each of these blocked the whole QGIS
  window until it finished, up to half an hour for a large raster
  upload.
- **Safer failure behavior across the board.** A publish that fails
  or is cancelled partway no longer leaves an orphaned half-created
  item on the portal (the plugin deletes it and says so). Offline
  clone writes to a temporary file and swaps it into place only on
  success, so a failed download can no longer destroy an existing
  GeoPackage. Push edits records the portal ids of created
  features, so retrying after a partial failure updates them
  instead of creating duplicates.
- **QGIS 4 / Qt 6 compatibility.** Enum access and file-writer
  error handling that QGIS 4 under Qt 6 removed or relocated are
  resolved at runtime with QGIS 3 fallbacks, including a case that
  broke plugin load outright. Log handlers no longer stack up
  across plugin reloads.

### Added

- **Private and org-shared layers render on the canvas.** At
  sign-in the plugin mints a read-only portal API key, stores it in
  the QGIS authentication database, and attaches it to non-public
  layer URIs so those layers actually draw. The key is revoked at
  sign-out and on connection delete. Read-only means a leaked layer
  URI cannot modify data; edits always use your signed-in session.

### Changed

- `gratisgis_client` was rewritten as a synchronous, dependency-free
  library (urllib transport, frozen dataclass models). Its public
  shape changed; it serves the plugin, and the standalone GratisGIS
  Python SDK lives in the main repo.

### Known limitations

- Private non-spatial tables do not render live; clone them to
  view and edit (the portal has no authenticated features surface
  for tables yet).
- Adding a layer from the search dock uses the public surface, so
  private layers added that way draw empty; add them from the
  Browser tree instead.
- tile_layer items are served from the public surface only, so
  private tile layers do not draw.

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
