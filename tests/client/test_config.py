# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PortalConfig."""

from __future__ import annotations

import pytest

from gratisgis_client.config import PortalConfig


def test_strips_trailing_slashes() -> None:
    c = PortalConfig(
        portal_url="https://portal.example.com/",
        keycloak_url="https://auth.example.com//",
    )
    assert c.portal_url == "https://portal.example.com"
    assert c.keycloak_url == "https://auth.example.com"


def test_api_base_path() -> None:
    c = PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
    )
    assert c.api_base == "https://portal.example.com/api"


def test_oidc_issuer_path() -> None:
    c = PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
        realm="my-realm",
    )
    assert c.oidc_issuer == "https://auth.example.com/realms/my-realm"


def test_missing_scheme_rejected() -> None:
    with pytest.raises(ValueError):
        PortalConfig(
            portal_url="portal.example.com",
            keycloak_url="https://auth.example.com",
        )


def test_unsupported_scheme_rejected() -> None:
    with pytest.raises(ValueError):
        PortalConfig(
            portal_url="ftp://portal.example.com",
            keycloak_url="https://auth.example.com",
        )


def test_missing_host_rejected() -> None:
    with pytest.raises(ValueError):
        PortalConfig(
            portal_url="https://",
            keycloak_url="https://auth.example.com",
        )


def test_default_scope_includes_offline_access() -> None:
    c = PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
    )
    assert "offline_access" in c.scope
    assert "openid" in c.scope


def test_default_user_agent_is_built_from_package_version() -> None:
    # Single-sourced: bumping __version__ must change the UA without
    # touching a hand-maintained string.
    from gratisgis_client import __version__

    c = PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
    )
    assert c.user_agent == f"gratisgis-client/{__version__}"
