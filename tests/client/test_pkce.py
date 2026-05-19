# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the PKCE challenge primitives.

The interactive flow (loopback server + browser launch) is not unit
tested here; it gets integration coverage in Phase 0's manual smoke
test against a real Keycloak.
"""

from __future__ import annotations

import base64
import hashlib

from gratisgis_client.auth.pkce import PKCEChallenge


def test_challenge_matches_sha256_of_verifier() -> None:
    pkce = PKCEChallenge.generate()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pkce.verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert pkce.challenge == expected


def test_method_is_s256() -> None:
    assert PKCEChallenge.generate().method == "S256"


def test_verifier_is_within_rfc7636_bounds() -> None:
    # RFC 7636: verifier is 43-128 chars of [A-Z][a-z][0-9]-._~
    pkce = PKCEChallenge.generate()
    assert 43 <= len(pkce.verifier) <= 128
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    assert all(c in allowed for c in pkce.verifier)


def test_each_generation_is_unique() -> None:
    seen = {PKCEChallenge.generate().verifier for _ in range(50)}
    assert len(seen) == 50
