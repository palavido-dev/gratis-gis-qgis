# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the portal discovery flow."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from gratisgis_client import (
    PortalDiscoveryError,
    PortalInfo,
    discover,
    portal_config_from_discovery,
)

_GOOD_RESPONSE = {
    "name": "GratisGIS Demo",
    "version": "0.1.0",
    "api": {"baseUrl": "http://localhost:3000/api"},
    "auth": {
        "type": "oidc",
        "issuer": "http://localhost:8080/realms/gratis-gis",
    },
}


@pytest.mark.asyncio
async def test_discover_parses_valid_response(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:3000/api/portal-info",
        json=_GOOD_RESPONSE,
    )
    result = await discover("http://localhost:3000")
    assert result.info.name == "GratisGIS Demo"
    assert result.info.api.base_url == "http://localhost:3000/api"
    assert result.info.auth.type == "oidc"
    assert result.info.auth.issuer == "http://localhost:8080/realms/gratis-gis"
    # Canonical URL falls back to the user-supplied base when the
    # mock response doesn't carry a final URL with the suffix.
    assert result.portal_url == "http://localhost:3000"


@pytest.mark.asyncio
async def test_discover_strips_trailing_slash(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:3000/api/portal-info",
        json=_GOOD_RESPONSE,
    )
    # User typed the URL with a trailing slash; we should still hit
    # the right endpoint (no double-slash, no missed match).
    result = await discover("http://localhost:3000/")
    assert result.info.name == "GratisGIS Demo"


@pytest.mark.asyncio
async def test_discover_404_raises_portal_discovery_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:3000/api/portal-info",
        status_code=404,
    )
    with pytest.raises(PortalDiscoveryError) as exc_info:
        await discover("http://localhost:3000")
    assert "404" in str(exc_info.value)
    assert exc_info.value.url.endswith("/api/portal-info")


@pytest.mark.asyncio
async def test_discover_invalid_json_raises_portal_discovery_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="http://localhost:3000/api/portal-info",
        text="<html>Not a portal</html>",
        headers={"Content-Type": "text/html"},
    )
    with pytest.raises(PortalDiscoveryError):
        await discover("http://localhost:3000")


@pytest.mark.asyncio
async def test_discover_malformed_response_raises(httpx_mock: HTTPXMock) -> None:
    # 200 OK but missing required fields.
    httpx_mock.add_response(
        url="http://localhost:3000/api/portal-info",
        json={"name": "Half a portal"},
    )
    with pytest.raises(PortalDiscoveryError):
        await discover("http://localhost:3000")


@pytest.mark.asyncio
async def test_discover_network_error_raises_portal_discovery_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    with pytest.raises(PortalDiscoveryError) as exc_info:
        await discover("http://localhost:9999")
    assert "9999" in exc_info.value.url


def test_portal_config_from_discovery_splits_keycloak_issuer() -> None:
    info = PortalInfo.model_validate(_GOOD_RESPONSE)
    config = portal_config_from_discovery(
        portal_url="http://localhost:3000",
        info=info,
    )
    assert config.keycloak_url == "http://localhost:8080"
    assert config.realm == "gratis-gis"
    assert config.client_id == "qgis-plugin"
    assert config.portal_url == "http://localhost:3000"


def test_portal_config_from_discovery_rejects_non_keycloak_issuer() -> None:
    info = PortalInfo.model_validate(
        {
            **_GOOD_RESPONSE,
            "auth": {"type": "oidc", "issuer": "https://accounts.google.com"},
        }
    )
    with pytest.raises(PortalDiscoveryError):
        portal_config_from_discovery(
            portal_url="http://localhost:3000",
            info=info,
        )


def test_portal_config_from_discovery_accepts_custom_client_id() -> None:
    info = PortalInfo.model_validate(_GOOD_RESPONSE)
    config = portal_config_from_discovery(
        portal_url="http://localhost:3000",
        info=info,
        client_id="custom-client",
    )
    assert config.client_id == "custom-client"
