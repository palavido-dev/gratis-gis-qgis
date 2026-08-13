# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the personal API keys endpoint wrapper.

The wire shape matters more than usual here: the QGIS plugin's
private-layer rendering depends on ``readOnly: true`` actually
reaching the portal (a read-write key in a layer URI would be a
leakable write credential), and on the token surfacing exactly once
from create.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.api_keys import ApiKeysEndpoint
from gratisgis_client.http import PortalHttp
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


def _endpoint(transport: FakeTransport) -> ApiKeysEndpoint:
    config = PortalConfig(
        portal_url="https://portal.example",
        keycloak_url="https://portal.example",
        realm="gratis-gis",
        client_id="qgis-plugin",
    )
    return ApiKeysEndpoint(PortalHttp(config, _FakeAuth(), transport=transport))


def _summary_payload(id_: str = "key-1", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": id_,
        "name": "QGIS layers (demo)",
        "prefix": "ggk_abc1",
        "readOnly": True,
        "expiresAt": None,
        "lastUsedAt": None,
        "revokedAt": None,
        "createdAt": "2026-08-13T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestList:
    def test_parses_array_of_summaries(self) -> None:
        transport = FakeTransport().add(
            json_response([_summary_payload("a"), _summary_payload("b", readOnly=False)])
        )
        keys = _endpoint(transport).list()

        assert path_of(transport.requests[0]) == "/api/users/me/api-keys"
        assert transport.requests[0].method == "GET"
        assert [k.id for k in keys] == ["a", "b"]
        assert keys[0].read_only is True
        assert keys[1].read_only is False

    def test_list_results_never_carry_a_token(self) -> None:
        # The token exists only on the create response; a summary
        # growing one would mean the portal started echoing secrets.
        transport = FakeTransport().add(json_response([_summary_payload()]))
        [key] = _endpoint(transport).list()
        assert not hasattr(key, "token")

    def test_non_array_body_raises(self) -> None:
        transport = FakeTransport().add(json_response({"items": []}))
        with pytest.raises(ValueError):
            _endpoint(transport).list()

    def test_parses_nullable_timestamps(self) -> None:
        transport = FakeTransport().add(
            json_response(
                [
                    _summary_payload(
                        expiresAt="2027-08-13T00:00:00Z",
                        lastUsedAt="2026-08-13T12:00:00Z",
                        revokedAt=None,
                    )
                ]
            )
        )
        [key] = _endpoint(transport).list()
        assert isinstance(key.expires_at, datetime)
        assert key.expires_at.year == 2027
        assert isinstance(key.last_used_at, datetime)
        assert key.revoked_at is None
        assert key.created_at.tzinfo is not None


class TestCreate:
    def test_sends_name_read_only_and_expiry(self) -> None:
        transport = FakeTransport().add(
            json_response({**_summary_payload("new"), "token": "ggk_secret"})
        )
        created = _endpoint(transport).create(
            name="QGIS layers (demo)", read_only=True, expires_in_days=365
        )

        sent = transport.requests[0]
        assert sent.method == "POST"
        assert path_of(sent) == "/api/users/me/api-keys"
        # readOnly true is the load-bearing bit: a leaked layer URI
        # must never carry a write-capable credential.
        assert body_json(sent) == {
            "name": "QGIS layers (demo)",
            "readOnly": True,
            "expiresInDays": 365,
        }
        assert created.id == "new"

    def test_token_surfaces_on_create(self) -> None:
        transport = FakeTransport().add(
            json_response({**_summary_payload("new"), "token": "ggk_secret"})
        )
        created = _endpoint(transport).create(name="k", read_only=True)
        assert created.token == "ggk_secret"

    def test_create_without_token_in_response_raises(self) -> None:
        # A create response with no token is unusable (the token is
        # unrecoverable later); fail loudly instead of storing an
        # empty Authorization header.
        transport = FakeTransport().add(json_response(_summary_payload("new")))
        with pytest.raises(ValueError):
            _endpoint(transport).create(name="k")

    def test_omits_expiry_when_none(self) -> None:
        # Absent means "until revoked" server-side; an explicit null
        # would be rejected by the portal's DTO validation.
        transport = FakeTransport().add(
            json_response({**_summary_payload("new"), "token": "ggk_x"})
        )
        _endpoint(transport).create(name="k", read_only=False)
        sent = body_json(transport.requests[0])
        assert sent == {"name": "k", "readOnly": False}


class TestRevoke:
    def test_deletes_by_id_and_parses_summary(self) -> None:
        transport = FakeTransport().add(
            json_response(_summary_payload("key-9", revokedAt="2026-08-13T09:00:00Z"))
        )
        revoked = _endpoint(transport).revoke("key-9")

        sent = transport.requests[0]
        assert sent.method == "DELETE"
        assert path_of(sent) == "/api/users/me/api-keys/key-9"
        assert revoked.id == "key-9"
        assert isinstance(revoked.revoked_at, datetime)
