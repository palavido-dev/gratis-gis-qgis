# GratisGIS for QGIS

A QGIS plugin that makes GratisGIS portals feel native inside QGIS.
Browse portal content from the QGIS Browser panel, drag layers and
maps to the canvas, publish vector and raster data, edit hosted
feature layers with optimistic sync and a first-class conflict
dialog, and work offline against cached items.

**Status:** Pre-alpha. Phase 0 (foundation) in progress. Not yet
installable from the QGIS Plugin Repository.

**License:** AGPL-3.0-or-later. Same as GratisGIS itself.

**Target QGIS version:** 3.34 LTS or newer.

## Repo layout

```
src/
  gratisgis_client/     Pure Python portal API client. No QGIS deps.
                        Publishable on PyPI; reusable in CLI scripts
                        and Jupyter notebooks. Async (httpx).
  gratisgis_qgis/       The QGIS plugin proper. Depends on PyQGIS
                        and PyQt5. Wraps the client for QGIS-specific
                        concerns (auth manager bridge, providers,
                        Browser panel, dialogs, tasks).
tests/
  client/               Unit tests for the client (no QGIS needed).
  qgis/                 Tests that run inside QGIS (pytest-qgis).
docs/
  development.md        How to set up a dev environment and load the
                        plugin into QGIS without packaging it.
```

## Design

See [`docs/handoff/qgis-plugin-plan.md`](https://github.com/palavido-dev/gratis-gis/blob/main/docs/handoff/qgis-plugin-plan.md)
in the main GratisGIS repo for the full plan, architectural
decisions, capability phasing, and open questions. That doc is the
source of truth; this README is the entry point.

## Quick development setup

```
pip install -e .[dev]
pytest tests/client
```

To run the plugin inside QGIS during development, symlink (or copy)
`src/gratisgis_qgis` into your QGIS user plugin directory and reload
plugins. Full instructions in [`docs/development.md`](docs/development.md).

## Authentication

The plugin uses OIDC PKCE flow against the Keycloak realm bundled
with each GratisGIS deployment. Tokens are stored in the QGIS auth
manager (encrypted, the same store QGIS uses for PostgreSQL and
WFS credentials). Token refresh is automatic.

From the user's point of view, signing in is one field. The
connection dialog asks only for the portal URL; the plugin fetches
`<portal>/api/portal-info` to discover the OIDC issuer, the API
base URL, and the portal's display name, then runs the PKCE flow.
Keycloak realm names, client IDs, and loopback ports never appear
in the UI.

The plugin sends `client_id=qgis-plugin` on every PKCE handshake.
That client ships in the realm export under `infra/keycloak/` in
the main gratis-gis repo, with both the custom scheme
`gratisgis-qgis://auth-callback` and the loopback prefix
`http://127.0.0.1*` registered as valid redirect URIs. Re-import
the realm after pulling the latest GratisGIS so a fresh deployment
picks the client up.

## Contributing

This is a solo project today. PRs welcome once Phase 0 lands and
the foundation is stable enough to build on without churn.

