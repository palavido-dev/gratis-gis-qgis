# Contributing

GratisGIS for QGIS is in Phase 0 right now (foundation). The plugin
isn't useful yet; the codebase will churn meaningfully until Phase 1
(browse + add to canvas) lands. Once that ships, this doc gets a real
contribution guide.

## In the meantime

- Open issues for design discussion: terminology, browser-tree shape,
  publish dialog flow, symbology subset.
- Open issues for bugs you find in the development setup.
- File feature requests against [the main GratisGIS repo](https://github.com/palavido-dev/gratis-gis)
  if they describe portal behavior, not plugin behavior.

## Development setup

See [`docs/development.md`](docs/development.md).

## Coding conventions

- Python 3.10+.
- Type-annotate everything in `src/gratisgis_client`. The pure-Python
  client is `mypy --strict` clean.
- `src/gratisgis_qgis` is more permissive with types because PyQGIS
  stubs are not always reliable, but new code should still type-annotate.
- License header at the top of every file:
  `# SPDX-License-Identifier: AGPL-3.0-or-later`
- Conventional Commits in commit messages.
- No em dashes or double hyphens anywhere in committed text.
- No "Co-Authored-By" or AI attribution in commit messages.
