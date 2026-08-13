# SPDX-License-Identifier: AGPL-3.0-or-later
"""PKCE (RFC 7636) flow against an OIDC authorization server.

The pure-Python implementation here uses a loopback HTTP server to
catch the authorization code redirect. It is suitable for CLI
scripts, notebooks, and any environment where launching the user's
default browser is acceptable.

``run`` blocks the calling thread until the callback lands, the
timeout expires, or the caller's ``cancel`` probe fires. The QGIS
plugin runs it inside a background task so the GUI thread never
waits on the browser.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse


def _b64url(raw: bytes) -> str:
    """Standard PKCE base64url encoding (no padding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PKCEChallenge:
    """A PKCE code verifier/challenge pair.

    ``verifier`` is the random secret kept private by the client.
    ``challenge`` is the SHA-256 hash of the verifier, base64url-encoded,
    which is included in the authorization request.

    On the token exchange, the client presents the verifier and the
    server checks ``SHA256(verifier) == challenge``.

    ``method`` is always ``"S256"``. Plain method is not implemented
    by design.
    """

    verifier: str
    challenge: str
    method: str = "S256"

    @classmethod
    def generate(cls) -> PKCEChallenge:
        """Generate a fresh verifier and matching challenge.

        Verifier is 96 random bytes encoded as 128 chars of base64url.
        That comfortably exceeds RFC 7636's 43-char minimum without
        bumping into the 128-char maximum.
        """
        verifier_bytes = secrets.token_bytes(96)
        verifier = _b64url(verifier_bytes)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        return cls(verifier=verifier, challenge=challenge)


def _pick_free_port() -> int:
    """Reserve a free localhost port and return its number.

    The port is closed before being returned, so there is a brief
    window where another process could grab it. In practice the OS
    issues distinct ports rapidly enough that this is not a problem
    for an interactive flow, and Keycloak typically allows a glob
    on localhost redirect URIs anyway.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class _AuthorizationResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None


@dataclass
class _CallbackOutcome:
    """Mutable slot the handler thread writes the result into.

    A plain holder plus an ``Event`` instead of a Future: the waiting
    side is a normal thread, not an event loop.
    """

    result: _AuthorizationResult | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OIDC redirect.

    Subclassed per flow so that the handler's class attributes hold
    the outcome slot and completion event. This keeps the handler
    thread-safe without a module-global.
    """

    outcome: _CallbackOutcome
    done: threading.Event

    def log_message(self, format: str, *args: object) -> None:
        # The default handler logs to stderr, which clutters CLI use.
        return

    def do_GET(self) -> None:  # http.server API name
        parsed = urlparse(self.path)
        # Browsers request /favicon.ico from the loopback origin right
        # around the redirect. Only the /callback path may complete the
        # flow; anything else 404s without touching the outcome.
        if parsed.path != "/callback":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        result = _AuthorizationResult(
            code=(params["code"][0] if "code" in params else None),
            state=(params["state"][0] if "state" in params else None),
            error=(params["error"][0] if "error" in params else None),
            error_description=(
                params["error_description"][0] if "error_description" in params else None
            ),
        )
        body = _build_callback_html(result)
        body_bytes = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)
        # First callback wins; a stray re-load of the callback page
        # must not overwrite the captured code.
        if self.outcome.result is None:
            self.outcome.result = result
            self.done.set()


def _build_callback_html(result: _AuthorizationResult) -> str:
    if result.error:
        body = (
            f"<h1>Sign-in failed</h1>"
            f"<p>{result.error}</p>"
            f"<p>{result.error_description or ''}</p>"
            f"<p>You can close this window and try again in QGIS.</p>"
        )
    else:
        body = (
            "<h1>Signed in to GratisGIS</h1>"
            "<p>You can close this window and return to QGIS.</p>"
        )
    return (
        "<!doctype html><html><head>"
        "<meta charset='utf-8'>"
        "<title>GratisGIS sign-in</title>"
        "<style>"
        "body { font-family: -apple-system, system-ui, sans-serif; "
        "background:#f6f8fa; color:#0f172a; "
        "padding: 4rem 2rem; text-align: center; }"
        "h1 { font-size: 1.4rem; margin-bottom: 1rem; }"
        "p { color:#475569; margin: 0.5rem 0; }"
        "</style></head><body>"
        f"{body}"
        "</body></html>"
    )


class PKCEFlow:
    """Drives a PKCE authorization code flow against an OIDC server.

    The flow does not itself exchange the authorization code for
    tokens. That happens in ``AuthManager`` so the PKCE machinery
    is independent of which token endpoint we hit and what we do
    with the resulting tokens.
    """

    def __init__(
        self,
        *,
        authorization_endpoint: str,
        client_id: str,
        scope: tuple[str, ...],
        redirect_port: int = 0,
        browser_opener: Callable[[str], object] = webbrowser.open,
    ) -> None:
        self.authorization_endpoint = authorization_endpoint
        self.client_id = client_id
        self.scope = scope
        self.redirect_port = redirect_port
        self._browser_opener = browser_opener

    def run(
        self,
        *,
        timeout: float = 300.0,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[str, PKCEChallenge, str]:
        """Run the flow, blocking until the browser round-trip lands.

        Returns ``(authorization_code, pkce, redirect_uri)``.
        ``authorization_code`` is what the token exchange step needs.
        ``pkce`` carries the verifier the token exchange must present.
        ``redirect_uri`` is the same one the auth server saw, also
        required by the token exchange.

        ``cancel`` is polled every 0.25 s; when it returns ``True``
        the flow aborts with ``RuntimeError``, which is how a caller
        (a QGIS task's cancel button) interrupts a wait it cannot
        otherwise reach into.

        Raises ``TimeoutError`` if no callback arrives within
        ``timeout`` seconds. Raises ``RuntimeError`` on PKCE error
        responses; callers should map this to an ``AuthError`` at
        a higher layer.
        """
        port = self.redirect_port or _pick_free_port()
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        state = secrets.token_urlsafe(32)
        pkce = PKCEChallenge.generate()

        outcome = _CallbackOutcome()
        done = threading.Event()
        handler_cls = type(
            "_CallbackHandlerBound",
            (_CallbackHandler,),
            {"outcome": outcome, "done": done},
        )
        server = HTTPServer(("127.0.0.1", port), handler_cls)
        thread = threading.Thread(
            target=server.serve_forever, name="gratisgis-pkce-loopback", daemon=True
        )
        thread.start()
        try:
            params = {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self.scope),
                "state": state,
                "code_challenge": pkce.challenge,
                "code_challenge_method": pkce.method,
            }
            auth_url = f"{self.authorization_endpoint}?{urlencode(params)}"
            self._browser_opener(auth_url)

            deadline = time.monotonic() + timeout
            while not done.wait(0.25):
                if cancel is not None and cancel():
                    raise RuntimeError("Sign-in cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"No PKCE callback received within {timeout:.0f}s"
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        result = outcome.result
        if result is None:
            # Defensive: the event only sets after the slot is filled,
            # so this indicates a bug rather than a flow outcome.
            raise RuntimeError("Authorization callback produced no result")
        if result.error:
            raise RuntimeError(
                f"Authorization failed: {result.error} ({result.error_description or ''})"
            )
        if result.state != state:
            # State mismatch indicates a possible CSRF or a stale
            # callback from a different flow. Either way, refuse.
            raise RuntimeError("PKCE state mismatch (possible CSRF)")
        if not result.code:
            raise RuntimeError("Authorization callback missing 'code' parameter")

        return result.code, pkce, redirect_uri
