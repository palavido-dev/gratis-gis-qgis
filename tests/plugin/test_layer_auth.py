# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in / sign-out key lifecycle at the client's endpoint seam.

``layer_auth`` owns the requests the connection dialog sends when it
mints and revokes the per-connection layer-rendering key; these
tests drive it through a real ``ApiKeysEndpoint`` over the fake
transport so the wire shape (readOnly true, the key name, the expiry
safety net) is pinned where it is produced.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.api_keys import ApiKeysEndpoint
from gratisgis_client.http import PortalHttp
from gratisgis_qgis.layer_auth import (
    LAYER_KEY_EXPIRES_DAYS,
    layer_key_name,
    mint_layer_key,
    revoke_layer_key,
)
from tests.client.transport_stub import (
    FakeTransport,
    body_json,
    json_response,
    path_of,
)


class _FakeAuth:
    def access_token(self) -> str:
        return "fake-token"

    def force_refresh(self) -> str:  # pragma: no cover
        return "fake-token"


def _client(transport: FakeTransport) -> Any:
    """A client-shaped object whose api_keys endpoint is real.

    ``mint_layer_key`` / ``revoke_layer_key`` only touch
    ``client.api_keys``, so a SimpleNamespace around the genuine
    endpoint keeps the test at the transport seam without standing up
    auth state for a full GratisGISClient.
    """
    config = PortalConfig(
        portal_url="https://portal.example",
        keycloak_url="https://portal.example",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )
    endpoint = ApiKeysEndpoint(PortalHttp(config, _FakeAuth(), transport=transport))
    return SimpleNamespace(api_keys=endpoint)


def _created_payload(id_: str = "key-1") -> dict[str, object]:
    return {
        "id": id_,
        "name": "QGIS layers (demo)",
        "prefix": "ggk_abc1",
        "readOnly": True,
        "expiresAt": "2027-08-13T00:00:00Z",
        "lastUsedAt": None,
        "revokedAt": None,
        "createdAt": "2026-08-13T00:00:00Z",
        "token": "ggk_secret_token",
    }


class TestMint:
    def test_mints_read_only_named_key_with_expiry(self) -> None:
        transport = FakeTransport().add(json_response(_created_payload()))
        minted = mint_layer_key(_client(transport), "demo")

        sent = transport.requests[0]
        assert sent.method == "POST"
        assert path_of(sent) == "/api/users/me/api-keys"
        assert body_json(sent) == {
            # The name carries the QGIS connection name so the user
            # can recognize (and safely revoke) it in the portal UI.
            "name": "QGIS layers (demo)",
            # Load-bearing: a leaked layer URI must never carry a
            # write-capable credential.
            "readOnly": True,
            # Safety net for keys whose revoke path never runs.
            "expiresInDays": LAYER_KEY_EXPIRES_DAYS,
        }
        # The token surfaces exactly once, here; the caller stores it
        # into the authcfg and never sees it again.
        assert minted.token == "ggk_secret_token"
        assert minted.id == "key-1"

    def test_key_name_embeds_profile_name(self) -> None:
        assert layer_key_name("My Portal") == "QGIS layers (My Portal)"


class TestRevoke:
    def test_revokes_by_id(self) -> None:
        payload = dict(_created_payload("key-9"))
        payload.pop("token")
        payload["revokedAt"] = "2026-08-13T09:00:00Z"
        transport = FakeTransport().add(json_response(payload))

        assert revoke_layer_key(_client(transport), "key-9") is True
        sent = transport.requests[0]
        assert sent.method == "DELETE"
        assert path_of(sent) == "/api/users/me/api-keys/key-9"

    def test_empty_id_is_a_noop(self) -> None:
        transport = FakeTransport()
        assert revoke_layer_key(_client(transport), "") is False
        assert transport.requests == []

    def test_server_error_is_swallowed(self) -> None:
        # Every caller is on a teardown path (sign-out, delete,
        # pre-mint cleanup) that must proceed even when the portal
        # says no; the portal answers 401 for unknown ids on purpose.
        # Two planned 401s because the http layer retries a 401 once
        # after a token refresh.
        transport = (
            FakeTransport()
            .add(json_response({"message": "Invalid API key."}, status=401))
            .add(json_response({"message": "Invalid API key."}, status=401))
        )
        assert revoke_layer_key(_client(transport), "key-9") is False
