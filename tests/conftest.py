# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared test fixtures."""

from __future__ import annotations

import pytest

from gratisgis_client.config import PortalConfig


@pytest.fixture
def portal_config() -> PortalConfig:
    return PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )
