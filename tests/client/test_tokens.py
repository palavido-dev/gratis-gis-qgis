# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the TokenSet dataclass."""

from __future__ import annotations

import dataclasses
import time

import pytest

from gratisgis_client.auth.tokens import TokenSet, jwt_subject


def _now() -> float:
    return 1_700_000_000.0


def _sample_response() -> dict[str, object]:
    return {
        "access_token": "access-xyz",
        "refresh_token": "refresh-xyz",
        "expires_in": 300,
        "refresh_expires_in": 3600,
        "token_type": "Bearer",
        "id_token": "id-xyz",
        "scope": "openid profile",
    }


def test_from_token_response_sets_expiries_relative_to_now() -> None:
    tokens = TokenSet.from_token_response(_sample_response(), now=_now())
    assert tokens.access_token == "access-xyz"
    assert tokens.refresh_token == "refresh-xyz"
    assert tokens.access_expires_at == _now() + 300
    assert tokens.refresh_expires_at == _now() + 3600
    assert tokens.id_token == "id-xyz"
    assert tokens.scope == "openid profile"


def test_from_token_response_defaults_refresh_to_access_when_missing() -> None:
    body = _sample_response()
    del body["refresh_expires_in"]
    tokens = TokenSet.from_token_response(body, now=_now())
    assert tokens.refresh_expires_at == _now() + 300


def test_from_token_response_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        TokenSet.from_token_response({"foo": "bar"}, now=_now())


def test_access_is_stale_uses_leeway() -> None:
    # Token expires at now+30; leeway is 30 by default, so it should be stale.
    tokens = TokenSet(
        access_token="a",
        refresh_token="r",
        access_expires_at=time.time() + 30,
        refresh_expires_at=time.time() + 3600,
    )
    assert tokens.access_is_stale() is True


def test_access_is_not_stale_when_well_before_expiry() -> None:
    tokens = TokenSet(
        access_token="a",
        refresh_token="r",
        access_expires_at=time.time() + 600,
        refresh_expires_at=time.time() + 3600,
    )
    assert tokens.access_is_stale() is False


def test_refresh_is_stale_when_in_the_past() -> None:
    tokens = TokenSet(
        access_token="a",
        refresh_token="r",
        access_expires_at=time.time() - 100,
        refresh_expires_at=time.time() - 1,
    )
    assert tokens.refresh_is_stale() is True


def test_offline_token_refresh_in_zero_means_never_expires() -> None:
    # Keycloak returns refresh_expires_in=0 for offline-access tokens
    # (the offline_access scope), meaning "never expires" per the
    # OAuth offline-token spec. Without the special-case, a literal
    # `t0 + 0` would mark the refresh token stale the instant it was
    # saved and force interactive re-sign-in on the very next call.
    body = _sample_response()
    body["refresh_expires_in"] = 0
    tokens = TokenSet.from_token_response(body, now=_now())
    assert tokens.refresh_is_stale() is False
    # And it stays not-stale well into the future.
    assert tokens.refresh_expires_at > _now() + 10 * 365 * 24 * 3600


def test_offline_token_with_negative_refresh_in_also_treated_as_never() -> None:
    # Defensive: if a proxy ever rewrites the response to a negative
    # number, treat it the same as zero (no expiry) rather than
    # passing a past timestamp through.
    body = _sample_response()
    body["refresh_expires_in"] = -1
    tokens = TokenSet.from_token_response(body, now=_now())
    assert tokens.refresh_is_stale() is False


def test_tokenset_is_frozen() -> None:
    tokens = TokenSet(
        access_token="a",
        refresh_token="r",
        access_expires_at=0.0,
        refresh_expires_at=0.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        tokens.access_token = "b"  # type: ignore[misc]


def _fake_jwt(payload: dict[str, object]) -> str:
    """Build an unsigned JWT-shaped token: header.payload.signature
    with base64url segments and the padding stripped, the way real
    issuers emit them.
    """
    import base64
    import json

    def seg(obj: dict[str, object]) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'RS256', 'typ': 'JWT'})}.{seg(payload)}.c2ln"


def test_jwt_subject_extracts_sub_claim() -> None:
    token = _fake_jwt({"sub": "user-123", "preferred_username": "matt"})
    assert jwt_subject(token) == "user-123"


def test_jwt_subject_returns_none_for_opaque_or_malformed_tokens() -> None:
    assert jwt_subject("not-a-jwt") is None
    assert jwt_subject("a.b") is None
    assert jwt_subject("a.!!notbase64!!.c") is None
    # Decodable payload, but no sub claim in it.
    assert jwt_subject(_fake_jwt({})) is None


def test_tokenset_subject_prefers_access_token_then_id_token() -> None:
    with_access_sub = TokenSet(
        access_token=_fake_jwt({"sub": "from-access"}),
        refresh_token="r",
        access_expires_at=0.0,
        refresh_expires_at=0.0,
        id_token=_fake_jwt({"sub": "from-id"}),
    )
    assert with_access_sub.subject() == "from-access"

    opaque_access = TokenSet(
        access_token="opaque",
        refresh_token="r",
        access_expires_at=0.0,
        refresh_expires_at=0.0,
        id_token=_fake_jwt({"sub": "from-id"}),
    )
    assert opaque_access.subject() == "from-id"

    no_jwt_anywhere = TokenSet(
        access_token="opaque",
        refresh_token="r",
        access_expires_at=0.0,
        refresh_expires_at=0.0,
    )
    assert no_jwt_anywhere.subject() is None
