# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the portal discovery flow."""

from __future__ import annotations

import pytest

from gratisgis_client import (
    PortalDiscoveryError,
    PortalInfo,
    discover,
    portal_config_from_discovery,
)
from gratisgis_client.transport import TransportError, TransportRequest, TransportResponse
from tests.client.transport_stub import FakeTransport, json_response, text_response

_GOOD_RESPONSE = {
    "name": "GratisGIS Demo",
    "version": "0.1.0",
    "api": {"baseUrl": "http://localhost:3000/api"},
    "auth": {
        "type": "oidc",
        "issuer": "http://localhost:8080/realms/gratis-gis",
    },
}


def test_discover_parses_valid_response() -> None:
    transport = FakeTransport().add(json_response(_GOOD_RESPONSE))
    result = discover("http://localhost:3000", transport=transport)
    assert result.info.name == "GratisGIS Demo"
    assert result.info.api.base_url == "http://localhost:3000/api"
    assert result.info.auth.type == "oidc"
    assert result.info.auth.issuer == "http://localhost:8080/realms/gratis-gis"
    # No redirect happened, so the canonical URL is the input.
    assert result.portal_url == "http://localhost:3000"
    # And the probe hit the documented discovery route.
    assert transport.requests[0].url == "http://localhost:3000/api/portal-info"


def test_discover_strips_trailing_slash() -> None:
    # User typed the URL with a trailing slash; we should still hit
    # the right endpoint (no double-slash, no missed match).
    transport = FakeTransport().add(json_response(_GOOD_RESPONSE))
    result = discover("http://localhost:3000/", transport=transport)
    assert result.info.name == "GratisGIS Demo"
    assert transport.requests[0].url == "http://localhost:3000/api/portal-info"


def test_discover_canonicalizes_from_post_redirect_url() -> None:
    # A www / no-www 301 lands the probe on the canonical host; the
    # caller should be handed that host, not the user-typed one.
    transport = FakeTransport().add(
        json_response(_GOOD_RESPONSE, url="https://gratisgis.org/api/portal-info")
    )
    result = discover("https://www.gratisgis.org", transport=transport)
    assert result.portal_url == "https://gratisgis.org"


def test_discover_falls_back_to_input_when_final_url_is_unexpected() -> None:
    # A transport reporting a final URL without the discovery suffix
    # (redirected somewhere strange) must not corrupt the saved base.
    transport = FakeTransport().add(
        json_response(_GOOD_RESPONSE, url="https://elsewhere.example/landing")
    )
    result = discover("http://localhost:3000", transport=transport)
    assert result.portal_url == "http://localhost:3000"


def test_discover_404_raises_portal_discovery_error() -> None:
    transport = FakeTransport().add(json_response({}, status=404))
    with pytest.raises(PortalDiscoveryError) as exc_info:
        discover("http://localhost:3000", transport=transport)
    assert "404" in str(exc_info.value)
    assert exc_info.value.url.endswith("/api/portal-info")


def test_discover_invalid_json_raises_portal_discovery_error() -> None:
    transport = FakeTransport().add(
        text_response("<html>Not a portal</html>", content_type="text/html")
    )
    with pytest.raises(PortalDiscoveryError):
        discover("http://localhost:3000", transport=transport)


def test_discover_malformed_response_raises() -> None:
    # 200 OK but missing required fields.
    transport = FakeTransport().add(json_response({"name": "Half a portal"}))
    with pytest.raises(PortalDiscoveryError):
        discover("http://localhost:3000", transport=transport)


def test_discover_rejects_unknown_auth_backend() -> None:
    # PortalAuthInfo.type stays strict: an auth backend this client
    # cannot sign in against must fail at discovery, not at sign-in.
    transport = FakeTransport().add(
        json_response(
            {**_GOOD_RESPONSE, "auth": {"type": "saml", "issuer": "https://idp.example"}}
        )
    )
    with pytest.raises(PortalDiscoveryError):
        discover("http://localhost:3000", transport=transport)


def test_discover_network_error_raises_portal_discovery_error() -> None:
    transport = FakeTransport().add_exception(TransportError("connection refused"))
    with pytest.raises(PortalDiscoveryError) as exc_info:
        discover("http://localhost:9999", transport=transport)
    assert "9999" in exc_info.value.url


def test_discover_sends_user_agent_from_package_version() -> None:
    from gratisgis_client import __version__

    transport = FakeTransport().add(json_response(_GOOD_RESPONSE))
    discover("http://localhost:3000", transport=transport)
    request: TransportRequest = transport.requests[0]
    assert request.headers.get("User-Agent") == f"gratisgis-client/{__version__}"


def test_discover_ignores_unknown_top_level_fields() -> None:
    # Forward-compat parity with the old extra="ignore" models.
    transport = FakeTransport().add(
        json_response({**_GOOD_RESPONSE, "featureFlags": {"newThing": True}})
    )
    result = discover("http://localhost:3000", transport=transport)
    assert result.info.name == "GratisGIS Demo"


def test_portal_config_from_discovery_splits_keycloak_issuer() -> None:
    info = PortalInfo.from_api(_GOOD_RESPONSE)
    config = portal_config_from_discovery(
        portal_url="http://localhost:3000",
        info=info,
    )
    assert config.keycloak_url == "http://localhost:8080"
    assert config.realm == "gratis-gis"
    assert config.client_id == "qgis-plugin"
    assert config.portal_url == "http://localhost:3000"


def test_portal_config_from_discovery_rejects_non_keycloak_issuer() -> None:
    info = PortalInfo.from_api(
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
    info = PortalInfo.from_api(_GOOD_RESPONSE)
    config = portal_config_from_discovery(
        portal_url="http://localhost:3000",
        info=info,
        client_id="custom-client",
    )
    assert config.client_id == "custom-client"


def test_transport_response_defaults_url_to_request_url() -> None:
    # The stub mirrors a redirect-free transport by echoing the
    # request URL as the final URL; canonicalization then strips the
    # suffix exactly like it does against the real transport.
    transport = FakeTransport().add(
        TransportResponse(status=200, body=b'{"name": "n", "version": "1", "api": {"baseUrl": "x"}, "auth": {"type": "oidc", "issuer": "https://kc/realms/r"}}')
    )
    result = discover("http://localhost:3000", transport=transport)
    assert result.portal_url == "http://localhost:3000"
