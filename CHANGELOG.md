# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project tracks [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for the client library; the QGIS plugin uses its own version line in
`metadata.txt` so that QGIS Plugin Repository semantics are honored.

## [Unreleased]

### Added

- Initial repo scaffold: `gratisgis_client` portable Python client,
  `gratisgis_qgis` plugin shell, CI, tests, dev docs.
- PKCE OIDC auth flow against Keycloak.
- Connection management (add/edit/delete portal profiles).
- QGIS auth manager bridge for encrypted token storage.
- Plugin file logging.
- Keycloak realm dependency satisfied: a dedicated `qgis-plugin`
  public client now ships in the GratisGIS realm (PKCE S256, custom
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
