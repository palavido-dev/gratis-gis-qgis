# SPDX-License-Identifier: AGPL-3.0-or-later
"""AuthManager: orchestrates PKCE login, token storage, and refresh.

This is the single component that owns the active ``TokenSet`` for
a client instance. Everything else asks the manager "give me a fresh
access token" via ``access_token()``, and the manager handles:

- First-time interactive sign-in via PKCE
- Persisting and reloading tokens through a ``TokenStorage``
- Proactive refresh when the access token is near expiry
- Refresh-on-401 when the portal rejects a token mid-call

The manager is async; concurrent callers waiting on a refresh share
the same refresh request (no thundering-herd refreshes when many
requests notice a stale token at the same time).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from gratisgis_client.auth.pkce import PKCEFlow
from gratisgis_client.auth.storage import InMemoryTokenStorage, TokenStorage
from gratisgis_client.auth.tokens import TokenSet
from gratisgis_client.errors import AuthError

if TYPE_CHECKING:
    from gratisgis_client.config import PortalConfig

_log = logging.getLogger(__name__)


class AuthManager:
    """Manages tokens for one signed-in session against one portal.

    The manager owns an ``httpx.AsyncClient`` for hitting Keycloak's
    token endpoint. It does NOT own the portal-api client (that's
    threaded separately, with its own auth interceptor that asks
    this manager for the current access token).
    """

    def __init__(
        self,
        config: PortalConfig,
        *,
        storage: TokenStorage | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._storage: TokenStorage = storage if storage is not None else InMemoryTokenStorage()
        self._http = http  # If None, created lazily; closed in close()
        self._owns_http = http is None
        self._tokens: TokenSet | None = None
        self._loaded = False
        self._refresh_lock = asyncio.Lock()
        self._discovery: dict[str, object] | None = None

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                verify=self._config.verify_tls,
                headers={"User-Agent": self._config.user_agent},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._http

    async def close(self) -> None:
        """Close the manager's HTTP client if it owns one."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def discover(self) -> dict[str, object]:
        """Fetch and cache the OIDC discovery document.

        Cached for the lifetime of the manager. If the realm config
        changes server-side, you need to construct a new manager.
        """
        if self._discovery is None:
            http = await self._ensure_http()
            url = f"{self._config.oidc_issuer}/.well-known/openid-configuration"
            r = await http.get(url)
            if r.status_code != 200:
                raise AuthError(
                    f"OIDC discovery failed at {url}: HTTP {r.status_code}",
                    status=r.status_code,
                    body=r.text,
                )
            self._discovery = r.json()
        return self._discovery

    async def _load_persisted(self) -> None:
        if self._loaded:
            return
        self._tokens = await self._storage.load()
        self._loaded = True

    async def is_signed_in(self) -> bool:
        """True when we have tokens that can plausibly be used.

        A stale access token but a live refresh token still counts as
        signed in, since refresh-on-demand will recover.
        """
        await self._load_persisted()
        if self._tokens is None:
            return False
        return not self._tokens.refresh_is_stale()

    async def login_interactive(self, *, browser_opener=None, timeout: float = 300.0) -> TokenSet:  # type: ignore[no-untyped-def]
        """Run the PKCE flow and acquire a fresh token set.

        Replaces any existing tokens. The caller is responsible for
        deciding whether to call this; ``access_token()`` will raise
        ``AuthError`` if no tokens are present and won't trigger
        interactive sign-in on its own. This keeps "interactive"
        from happening inside a hot request path by accident.
        """
        discovery = await self.discover()
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
            code, pkce, redirect_uri = await flow.run(timeout=timeout)
        except TimeoutError as exc:
            raise AuthError(f"Sign-in timed out: {exc}") from exc
        except RuntimeError as exc:
            raise AuthError(f"Sign-in failed: {exc}") from exc

        tokens = await self._exchange_code(
            token_endpoint=token_endpoint,
            code=code,
            code_verifier=pkce.verifier,
            redirect_uri=redirect_uri,
        )
        self._tokens = tokens
        await self._storage.save(tokens)
        return tokens

    async def logout(self) -> None:
        """Clear the persisted tokens.

        We do not hit Keycloak's end-session endpoint here. The QGIS
        plugin's logout button does that as a separate step so the
        UI can decide whether to terminate the SSO session or only
        forget the local tokens.
        """
        await self._load_persisted()
        self._tokens = None
        await self._storage.clear()

    async def access_token(self) -> str:
        """Return a usable access token, refreshing if needed.

        Raises ``AuthError`` if no tokens are stored or if the refresh
        token has also expired. Callers that catch this should treat
        it as "the user must sign in again."
        """
        await self._load_persisted()
        if self._tokens is None:
            raise AuthError("Not signed in")

        if not self._tokens.access_is_stale():
            return self._tokens.access_token

        # Stale: refresh under a lock so concurrent callers share one refresh.
        async with self._refresh_lock:
            # Re-check after acquiring the lock; another coroutine may have
            # refreshed while we waited.
            if self._tokens is not None and not self._tokens.access_is_stale():
                return self._tokens.access_token
            await self._refresh_locked()
            assert self._tokens is not None  # _refresh_locked sets it
            return self._tokens.access_token

    async def force_refresh(self) -> str:
        """Refresh unconditionally and return the new access token.

        Useful as the recovery path when the portal returns 401 even
        though we thought our token was fresh. Refreshing under those
        circumstances covers clock skew and revoked-then-rotated
        scenarios.
        """
        await self._load_persisted()
        if self._tokens is None:
            raise AuthError("Not signed in")
        async with self._refresh_lock:
            await self._refresh_locked()
            assert self._tokens is not None
            return self._tokens.access_token

    async def _refresh_locked(self) -> None:
        assert self._tokens is not None
        if self._tokens.refresh_is_stale():
            raise AuthError("Refresh token expired; interactive sign-in required")

        discovery = await self.discover()
        token_endpoint = str(discovery["token_endpoint"])
        http = await self._ensure_http()
        data = {
            "grant_type": "refresh_token",
            "client_id": self._config.client_id,
            "refresh_token": self._tokens.refresh_token,
        }
        r = await http.post(token_endpoint, data=data)
        if r.status_code != 200:
            # 400 from Keycloak typically means refresh token revoked or
            # expired. Either way the user needs to sign in again.
            raise AuthError(
                f"Token refresh failed: HTTP {r.status_code}",
                status=r.status_code,
                body=_safe_json(r),
            )
        try:
            new_tokens = TokenSet.from_token_response(r.json())
        except ValueError as exc:
            raise AuthError(f"Malformed token response: {exc}") from exc
        self._tokens = new_tokens
        await self._storage.save(new_tokens)
        _log.debug("Refreshed access token; new expiry in %.0fs", new_tokens.access_expires_at)

    async def _exchange_code(
        self,
        *,
        token_endpoint: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> TokenSet:
        http = await self._ensure_http()
        data = {
            "grant_type": "authorization_code",
            "client_id": self._config.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
        r = await http.post(token_endpoint, data=data)
        if r.status_code != 200:
            body = _safe_json(r)
            # Log the body so plugin.log carries the actual Keycloak
            # reason (e.g. "Offline tokens not allowed for the user
            # or client"). The exception itself only carries the
            # short message; QMessageBox shows that, but operators
            # debugging from the log file need the structured detail.
            _log.warning(
                "Code exchange failed: HTTP %s body=%r", r.status_code, body
            )
            raise AuthError(
                f"Code exchange failed: HTTP {r.status_code}",
                status=r.status_code,
                body=body,
            )
        try:
            return TokenSet.from_token_response(r.json())
        except ValueError as exc:
            raise AuthError(f"Malformed token response: {exc}") from exc


def _safe_json(response: httpx.Response) -> object:
    """Return the response JSON if it parses, otherwise the raw text.

    Keycloak error responses are JSON in practice, but defensive
    parsing keeps us from masking the real error with a JSONDecodeError.
    """
    try:
        return response.json()
    except ValueError:
        return response.text
