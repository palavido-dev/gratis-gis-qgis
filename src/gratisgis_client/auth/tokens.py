# SPDX-License-Identifier: AGPL-3.0-or-later
"""Token data structures.

Kept deliberately small. ``TokenSet`` is immutable; refreshing
returns a new instance rather than mutating in place, so that the
client's "current tokens" reference is always a consistent snapshot
even while another thread is mid-refresh.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

# Sentinel "never expires" timestamp. Year 2286, well past any
# plausible install lifetime, chosen as a finite value so the
# persisted JSON stays standards-compliant (json.dumps of
# float('inf') emits the non-RFC `Infinity` literal which other
# JSON consumers reject). The token-refresh check treats any value
# this large as effectively never-expires.
_NEVER_EXPIRES_AT: float = 9_999_999_999.0


def jwt_subject(token: str) -> str | None:
    """Best-effort ``sub`` claim from a JWT, without verification.

    Decodes only the payload segment (base64url with the stripped
    padding restored). No signature check on purpose: the token
    arrived over TLS from the issuer we asked, no crypto dependency
    is available here, and the extracted id is used for display and
    client-side filtering, never for an access decision (the portal
    enforces those server-side on every call). Returns ``None`` for
    opaque or malformed tokens.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except ValueError:
        # Covers binascii.Error, JSONDecodeError, UnicodeDecodeError.
        return None
    if not isinstance(claims, dict):
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def _as_float(value: object) -> float:
    """Coerce a JSON-decoded value to ``float``.

    Keycloak's token endpoint encodes ``expires_in`` as a JSON number
    (so we expect ``int``), but the typed JSON view is ``object``;
    narrowing here keeps the dataclass strict and gives a clean
    error message if the field is something nonsensical.
    """
    if isinstance(value, (int, float, str)):
        return float(value)
    raise ValueError(f"expected numeric, got {type(value).__name__}")


@dataclass(frozen=True)
class TokenSet:
    """A complete set of tokens for one signed-in session.

    ``access_token`` is the JWT presented as ``Authorization: Bearer``
    on portal-api calls. ``refresh_token`` is presented to Keycloak's
    token endpoint to mint a new access token when the current one
    expires.

    ``access_expires_at`` and ``refresh_expires_at`` are unix
    timestamps (seconds, UTC). Treat both as best-effort: clocks
    drift, networks lie, the only authoritative answer is a 401 from
    the portal.

    ``id_token`` is the OIDC id token. Not strictly required for
    portal-api calls (those only need the access token) but the QGIS
    plugin uses claims from it for the connection profile's display
    name.
    """

    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float
    id_token: str | None = None
    token_type: str = "Bearer"
    scope: str | None = None

    def subject(self) -> str | None:
        """The signed-in user's id (the ``sub`` claim), or ``None``
        when neither token is a decodable JWT. Prefers the access
        token; falls back to the id token, which carries the same
        ``sub`` and covers hypothetical opaque-access-token setups.
        """
        sub = jwt_subject(self.access_token)
        if sub is None and self.id_token:
            sub = jwt_subject(self.id_token)
        return sub

    def access_is_stale(self, leeway: float = 30.0) -> bool:
        """True when the access token has expired or is about to.

        ``leeway`` is the buffer in seconds: refresh proactively that
        many seconds before the real expiry so we are not racing the
        clock on the round-trip.
        """
        return time.time() + leeway >= self.access_expires_at

    def refresh_is_stale(self, leeway: float = 30.0) -> bool:
        """True when the refresh token has expired or is about to.

        When the refresh token is stale, the user must re-authenticate
        interactively; refresh-on-401 cannot recover.
        """
        return time.time() + leeway >= self.refresh_expires_at

    @classmethod
    def from_token_response(cls, body: dict[str, object], *, now: float | None = None) -> TokenSet:
        """Construct a ``TokenSet`` from a Keycloak token endpoint response.

        The Keycloak response is a dict with ``access_token``,
        ``refresh_token``, ``expires_in`` (seconds until access
        expires), ``refresh_expires_in`` (seconds until refresh
        expires), ``token_type``, optional ``id_token``, optional
        ``scope``.
        """
        t0 = now if now is not None else time.time()
        try:
            access = str(body["access_token"])
            refresh = str(body["refresh_token"])
            access_in = _as_float(body["expires_in"])
            refresh_in = _as_float(body.get("refresh_expires_in", access_in))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed token response: missing/invalid field ({exc})") from exc

        # Keycloak returns refresh_expires_in == 0 for offline tokens
        # (the offline_access scope), meaning "never expires" per the
        # OAuth offline-token spec. A literal `t0 + 0` would mark the
        # refresh token stale the instant it was saved and force
        # interactive re-sign-in on the next API call. Treat 0 (and,
        # defensively, any negative value) as "no expiry" by using a
        # far-future sentinel timestamp.
        refresh_expires_at = (
            _NEVER_EXPIRES_AT if refresh_in <= 0 else t0 + refresh_in
        )

        return cls(
            access_token=access,
            refresh_token=refresh,
            access_expires_at=t0 + access_in,
            refresh_expires_at=refresh_expires_at,
            id_token=(str(body["id_token"]) if "id_token" in body else None),
            token_type=str(body.get("token_type", "Bearer")),
            scope=(str(body["scope"]) if "scope" in body else None),
        )
