# SPDX-License-Identifier: AGPL-3.0-or-later
"""AuthManager: orchestrates PKCE login, token storage, and refresh.

This is the single component that owns the active ``TokenSet`` for
a client instance. Everything else asks the manager "give me a fresh
access token" via ``access_token()``, and the manager handles:

- First-time interactive sign-in via PKCE
- Persisting and reloading tokens through a ``TokenStorage``
- Proactive refresh when the access token is near expiry
- Refresh-on-401 when the portal rejects a token mid-call

Calls are thread-safe: concurrent callers noticing a stale token at
the same time serialize on one lock, and whoever loses the race
re-checks and reuses the winner's refresh (no thundering-herd
refreshes).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from gratisgis_client.auth.pkce import PKCEFlow
from gratisgis_client.auth.storage import InMemoryTokenStorage, TokenStorage
from gratisgis_client.auth.tokens import TokenSet
from gratisgis_client.errors import AuthError
from gratisgis_client.transport import (
    Transport,
    TransportError,
    TransportRequest,
    TransportResponse,
    UrllibTransport,
)

if TYPE_CHECKING:
    from gratisgis_client.config import PortalConfig

_log = logging.getLogger(__name__)

# Keycloak round-trips are small JSON bodies; anything slower than
# this is a network problem the user should hear about promptly.
_AUTH_TIMEOUT = 30.0


class AuthManager:
    """Manages tokens for one signed-in session against one portal.

    The manager talks to Keycloak's endpoints through a ``Transport``.
    It does NOT own the portal-api transport (that's threaded
    separately through ``PortalHttp``, which asks this manager for the
    current access token).
    """

    def __init__(
        self,
        config: PortalConfig,
        *,
        storage: TokenStorage | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._config = config
        self._storage: TokenStorage = storage if storage is not None else InMemoryTokenStorage()
        self._transport: Transport = (
            transport
            if transport is not None
            else UrllibTransport(verify_tls=config.verify_tls)
        )
        self._tokens: TokenSet | None = None
        self._loaded = False
        self._refresh_lock = threading.Lock()
        self._discovery: dict[str, object] | None = None

    def close(self) -> None:
        """Release resources. Currently a no-op.

        Kept so the construct/close pairing in ``GratisGISClient``
        stays stable if the transport ever grows pooled connections.
        """

    def discover(self) -> dict[str, object]:
        """Fetch and cache the OIDC discovery document.

        Cached for the lifetime of the manager. If the realm config
        changes server-side, you need to construct a new manager.
        """
        if self._discovery is None:
            url = f"{self._config.oidc_issuer}/.well-known/openid-configuration"
            try:
                r = self._transport.send(
                    TransportRequest(
                        method="GET",
                        url=url,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": self._config.user_agent,
                        },
                        timeout=_AUTH_TIMEOUT,
                    )
                )
            except TransportError as exc:
                raise AuthError(f"OIDC discovery failed at {url}: {exc}") from exc
            if r.status != 200:
                raise AuthError(
                    f"OIDC discovery failed at {url}: HTTP {r.status}",
                    status=r.status,
                    body=r.text,
                )
            try:
                doc = r.json()
            except ValueError as exc:
                raise AuthError(f"OIDC discovery response at {url} is not JSON: {exc}") from exc
            if not isinstance(doc, dict):
                raise AuthError(f"OIDC discovery response at {url} is not an object")
            self._discovery = doc
        return self._discovery

    def _load_persisted(self) -> None:
        if self._loaded:
            return
        self._tokens = self._storage.load()
        self._loaded = True

    def is_signed_in(self) -> bool:
        """True when we have tokens that can plausibly be used.

        A stale access token but a live refresh token still counts as
        signed in, since refresh-on-demand will recover.
        """
        self._load_persisted()
        if self._tokens is None:
            return False
        return not self._tokens.refresh_is_stale()

    def login_interactive(
        self,
        *,
        browser_opener: Callable[[str], object] | None = None,
        timeout: float = 300.0,
        cancel: Callable[[], bool] | None = None,
    ) -> TokenSet:
        """Run the PKCE flow and acquire a fresh token set.

        Replaces any existing tokens. The caller is responsible for
        deciding whether to call this; ``access_token()`` will raise
        ``AuthError`` if no tokens are present and won't trigger
        interactive sign-in on its own. This keeps "interactive"
        from happening inside a hot request path by accident.

        ``cancel`` is forwarded to ``PKCEFlow.run`` so a background
        task can abort the browser wait from its cancel button.
        """
        discovery = self.discover()
        auth_endpoint = str(discovery["authorization_endpoint"])
        token_endpoint = str(discovery["token_endpoint"])

        flow_kwargs: dict[str, object] = {
            "authorization_endpoint": auth_endpoint,
            "client_id": self._config.client_id,
            "scope": self._config.scope,
            "redirect_port": self._config.redirect_port,
        }
        if browser_opener is not None:
            flow_kwargs["browser_opener"] = browser_opener
        flow = PKCEFlow(**flow_kwargs)  # type: ignore[arg-type]

        try:
            code, pkce, redirect_uri = flow.run(timeout=timeout, cancel=cancel)
        except TimeoutError as exc:
            raise AuthError(f"Sign-in timed out: {exc}") from exc
        except RuntimeError as exc:
            raise AuthError(f"Sign-in failed: {exc}") from exc

        tokens = self._exchange_code(
            token_endpoint=token_endpoint,
            code=code,
            code_verifier=pkce.verifier,
            redirect_uri=redirect_uri,
        )
        self._tokens = tokens
        self._storage.save(tokens)
        return tokens

    def logout(self) -> None:
        """Clear the persisted tokens.

        We do not hit Keycloak's end-session endpoint here. The QGIS
        plugin's logout button does that as a separate step so the
        UI can decide whether to terminate the SSO session or only
        forget the local tokens.
        """
        self._load_persisted()
        self._tokens = None
        self._storage.clear()

    def access_token(self) -> str:
        """Return a usable access token, refreshing if needed.

        Raises ``AuthError`` if no tokens are stored or if the refresh
        token has also expired. Callers that catch this should treat
        it as "the user must sign in again."
        """
        self._load_persisted()
        if self._tokens is None:
            raise AuthError("Not signed in")

        if not self._tokens.access_is_stale():
            return self._tokens.access_token

        # Stale: refresh under a lock so concurrent callers share one refresh.
        with self._refresh_lock:
            # Re-check after acquiring the lock; another thread may have
            # refreshed while we waited.
            if self._tokens is not None and not self._tokens.access_is_stale():
                return self._tokens.access_token
            self._refresh_locked()
            assert self._tokens is not None  # _refresh_locked sets it
            return self._tokens.access_token

    def force_refresh(self) -> str:
        """Refresh unconditionally and return the new access token.

        Useful as the recovery path when the portal returns 401 even
        though we thought our token was fresh. Refreshing under those
        circumstances covers clock skew and revoked-then-rotated
        scenarios.
        """
        self._load_persisted()
        if self._tokens is None:
            raise AuthError("Not signed in")
        with self._refresh_lock:
            self._refresh_locked()
            assert self._tokens is not None
            return self._tokens.access_token

    def _refresh_locked(self) -> None:
        assert self._tokens is not None
        if self._tokens.refresh_is_stale():
            raise AuthError("Refresh token expired; interactive sign-in required")

        discovery = self.discover()
        token_endpoint = str(discovery["token_endpoint"])
        r = self._post_form(
            token_endpoint,
            {
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "refresh_token": self._tokens.refresh_token,
            },
        )
        if r.status != 200:
            # 400 from Keycloak typically means refresh token revoked or
            # expired. Either way the user needs to sign in again.
            raise AuthError(
                f"Token refresh failed: HTTP {r.status}",
                status=r.status,
                body=_safe_json(r),
            )
        try:
            new_tokens = TokenSet.from_token_response(_token_body(r))
        except ValueError as exc:
            raise AuthError(f"Malformed token response: {exc}") from exc
        self._tokens = new_tokens
        self._storage.save(new_tokens)
        _log.debug(
            "Refreshed access token; new expiry in %.0fs",
            new_tokens.access_expires_at - time.time(),
        )

    def _exchange_code(
        self,
        *,
        token_endpoint: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> TokenSet:
        r = self._post_form(
            token_endpoint,
            {
                "grant_type": "authorization_code",
                "client_id": self._config.client_id,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
        )
        if r.status != 200:
            body = _safe_json(r)
            # Log the body so plugin.log carries the actual Keycloak
            # reason (e.g. "Offline tokens not allowed for the user
            # or client"). The exception itself only carries the
            # short message; QMessageBox shows that, but operators
            # debugging from the log file need the structured detail.
            _log.warning("Code exchange failed: HTTP %s body=%r", r.status, body)
            raise AuthError(
                f"Code exchange failed: HTTP {r.status}",
                status=r.status,
                body=body,
            )
        try:
            return TokenSet.from_token_response(_token_body(r))
        except ValueError as exc:
            raise AuthError(f"Malformed token response: {exc}") from exc

    def _post_form(self, url: str, form: dict[str, str]) -> TransportResponse:
        """Form-encoded POST to a Keycloak endpoint.

        Network-level failures become ``AuthError`` so callers of the
        sign-in and refresh paths never see a raw transport exception.
        """
        request = TransportRequest(
            method="POST",
            url=url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self._config.user_agent,
            },
            body=urlencode(form).encode("utf-8"),
            timeout=_AUTH_TIMEOUT,
        )
        try:
            return self._transport.send(request)
        except TransportError as exc:
            raise AuthError(f"Could not reach token endpoint at {url}: {exc}") from exc


def _safe_json(response: TransportResponse) -> object:
    """Return the response JSON if it parses, otherwise the raw text.

    Keycloak error responses are JSON in practice, but defensive
    parsing keeps us from masking the real error with a JSONDecodeError.
    """
    try:
        return response.json()
    except ValueError:
        return response.text


def _token_body(response: TransportResponse) -> dict[str, object]:
    """Parse a 200 token response body into the dict TokenSet expects."""
    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError(f"token endpoint returned non-JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("token endpoint returned a non-object JSON body")
    return body
