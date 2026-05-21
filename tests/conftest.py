# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared test fixtures."""

from __future__ import annotations

import os
import sys

# Make the QGIS-plugin package (`gratisgis_qgis`) importable from
# tests without installing it into the venv. The wheel
# `pyproject.toml` packages only `src/gratisgis_client`; the
# plugin code under `src/gratisgis_qgis` ships as a QGIS plugin
# zip, not as a PyPI distribution, but plugin-side tests still
# need to import its pure-Python modules (filtering, URL
# builders, etc.). Adding `src/` to sys.path lets pytest discover
# both packages without a parallel install step.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from gratisgis_client.config import PortalConfig  # noqa: E402


@pytest.fixture
def portal_config() -> PortalConfig:
    return PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )
