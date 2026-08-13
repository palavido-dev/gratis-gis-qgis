# SPDX-License-Identifier: AGPL-3.0-or-later
"""Personal API keys: list, create, revoke.

Wraps ``/api/users/me/api-keys`` (portal #219). The QGIS plugin
mints one READ-ONLY key per connection at sign-in and stores it in
a QGIS auth config so layer URIs can authenticate their tile
requests; the plugin-side orchestration lives in
``gratisgis_qgis.layer_auth``.

Two portal contract points worth pinning here:

- ``create`` is the only call that ever returns the plaintext token
  (prefix ``ggk_``); it is unrecoverable afterwards. ``list`` and
  ``revoke`` return metadata only.
- The portal refuses API-key callers on these routes outright (a
  credential must not mint credentials), so they only work on the
  interactive OIDC session.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gratisgis_client._parse import (
    opt_datetime,
    req_bool,
    req_datetime,
    req_str,
    require_dict,
)

if TYPE_CHECKING:
    from gratisgis_client.http import PortalHttp


@dataclass(frozen=True, kw_only=True)
class ApiKeySummary:
    """One key as the list / revoke responses describe it.

    Never carries the token; ``prefix`` is the recognizable
    ``ggk_``-prefixed head the portal keeps for display.
    """

    id: str
    name: str
    prefix: str
    read_only: bool
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ApiKeySummary:
        return cls(**_summary_kwargs(require_dict(data, "ApiKeySummary")))


def _summary_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Parsed constructor kwargs for the ``ApiKeySummary`` fields.

    Shared between ``ApiKeySummary.from_api`` and
    ``ApiKeyCreated.from_api`` so the alias map exists exactly once.
    """
    return {
        "id": req_str(payload, "id"),
        "name": req_str(payload, "name"),
        "prefix": req_str(payload, "prefix"),
        "read_only": req_bool(payload, "readOnly"),
        "expires_at": opt_datetime(payload, "expiresAt"),
        "last_used_at": opt_datetime(payload, "lastUsedAt"),
        "revoked_at": opt_datetime(payload, "revokedAt"),
        "created_at": req_datetime(payload, "createdAt"),
    }


@dataclass(frozen=True, kw_only=True)
class ApiKeyCreated(ApiKeySummary):
    """The create response: the summary plus the shown-once token."""

    token: str
    """The full plaintext key. Shown exactly once; the portal stores
    only a hash, so a caller that drops this value cannot get it
    back and must mint a new key."""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ApiKeyCreated:
        payload = require_dict(data, "ApiKeyCreated")
        kwargs = _summary_kwargs(payload)
        kwargs["token"] = req_str(payload, "token")
        return cls(**kwargs)


class ApiKeysEndpoint:
    """Wrapper over ``/api/users/me/api-keys``."""

    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    def list(self) -> builtins.list[ApiKeySummary]:
        """Every key the signed-in user holds, revoked ones included
        (the portal sorts active first)."""
        body = self._http.request_json("GET", "/users/me/api-keys")
        if not isinstance(body, list):
            raise ValueError("GET /users/me/api-keys: expected a JSON array")
        return [ApiKeySummary.from_api(row) for row in body]

    def create(
        self,
        *,
        name: str,
        read_only: bool = False,
        expires_in_days: int | None = None,
    ) -> ApiKeyCreated:
        """Mint a key. The result's ``token`` is shown exactly once.

        ``expires_in_days`` of ``None`` means "until revoked"; the
        portal caps explicit values at 730 (about two years).
        """
        payload: dict[str, Any] = {"name": name, "readOnly": read_only}
        if expires_in_days is not None:
            payload["expiresInDays"] = expires_in_days
        body = self._http.request_json("POST", "/users/me/api-keys", json=payload)
        return ApiKeyCreated.from_api(body)

    def revoke(self, key_id: str) -> ApiKeySummary:
        """Revoke a key by id.

        Idempotent server-side: revoking an already-revoked key keeps
        the original ``revoked_at`` timestamp. An unknown or
        foreign-owned id raises (the portal answers 401 so a caller
        can never learn whether someone else's id exists).
        """
        body = self._http.request_json("DELETE", f"/users/me/api-keys/{key_id}")
        return ApiKeySummary.from_api(body)
