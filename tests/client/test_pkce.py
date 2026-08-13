# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the PKCE challenge primitives and the loopback flow.

The flow tests run the real loopback server and deliver the callback
themselves over a real socket; only the browser is stubbed. That
proves the state check, the error paths, cancel, and timeout against
the exact machinery a sign-in uses.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from gratisgis_client.auth.pkce import PKCEChallenge, PKCEFlow


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


def _flow(browser_opener) -> PKCEFlow:  # type: ignore[no-untyped-def]
    return PKCEFlow(
        authorization_endpoint="https://auth.example/realms/gg/auth",
        client_id="qgis-plugin",
        scope=("openid", "offline_access"),
        browser_opener=browser_opener,
    )


def _auth_url_parts(auth_url: str) -> dict[str, str]:
    """The flow's authorization request parameters, flattened."""
    query = parse_qs(urlsplit(auth_url).query)
    return {key: values[0] for key, values in query.items()}


def test_flow_completes_on_matching_callback() -> None:
    """Happy path: browser 'redirects' to the loopback server with the
    code and the same state the flow sent out.
    """
    pages: list[bytes] = []

    def opener(auth_url: str) -> None:
        params = _auth_url_parts(auth_url)
        redirect = params["redirect_uri"]
        state = params["state"]
        # PKCE bits must be present in the authorization request.
        assert params["code_challenge_method"] == "S256"
        assert params["response_type"] == "code"
        with urllib.request.urlopen(
            f"{redirect}?code=abc-123&state={quote(state)}", timeout=5.0
        ) as response:
            pages.append(response.read())

    flow = _flow(opener)
    code, pkce, redirect_uri = flow.run(timeout=10.0)

    assert code == "abc-123"
    assert redirect_uri.startswith("http://127.0.0.1:")
    assert redirect_uri.endswith("/callback")
    # The challenge handed back is the one advertised to the server.
    assert hashlib.sha256(pkce.verifier.encode("ascii")).digest()
    assert b"Signed in to GratisGIS" in pages[0]


def test_flow_rejects_state_mismatch() -> None:
    # A stale or forged callback carrying the wrong state must be
    # refused (CSRF defense), not exchanged for tokens.
    def opener(auth_url: str) -> None:
        params = _auth_url_parts(auth_url)
        with urllib.request.urlopen(
            f"{params['redirect_uri']}?code=abc&state=WRONG", timeout=5.0
        ):
            pass

    flow = _flow(opener)
    with pytest.raises(RuntimeError, match="state mismatch"):
        flow.run(timeout=10.0)


def test_flow_surfaces_authorization_error() -> None:
    def opener(auth_url: str) -> None:
        params = _auth_url_parts(auth_url)
        with urllib.request.urlopen(
            f"{params['redirect_uri']}?error=access_denied"
            f"&error_description=user+said+no&state={quote(params['state'])}",
            timeout=5.0,
        ):
            pass

    flow = _flow(opener)
    with pytest.raises(RuntimeError, match="access_denied"):
        flow.run(timeout=10.0)


def test_flow_ignores_non_callback_paths() -> None:
    # Browsers fetch /favicon.ico from the loopback origin; that must
    # not complete (or fail) the flow. The real callback afterwards
    # still wins.
    def opener(auth_url: str) -> None:
        params = _auth_url_parts(auth_url)
        origin = params["redirect_uri"].rsplit("/", 1)[0]
        try:
            urllib.request.urlopen(f"{origin}/favicon.ico", timeout=5.0)
        except urllib.error.HTTPError as err:
            assert err.code == 404
        with urllib.request.urlopen(
            f"{params['redirect_uri']}?code=real&state={quote(params['state'])}",
            timeout=5.0,
        ):
            pass

    flow = _flow(opener)
    code, _, _ = flow.run(timeout=10.0)
    assert code == "real"


def test_flow_times_out_when_no_callback_arrives() -> None:
    flow = _flow(lambda url: None)
    with pytest.raises(TimeoutError):
        flow.run(timeout=0.4)


def test_flow_cancel_probe_aborts_the_wait() -> None:
    # The cancel callable is how a background task's cancel button
    # reaches into the blocking wait.
    flow = _flow(lambda url: None)
    with pytest.raises(RuntimeError, match="cancelled"):
        flow.run(timeout=30.0, cancel=lambda: True)
