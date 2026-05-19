# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exception hierarchy for the GratisGIS client.

All exceptions raised by this package inherit from ``GratisGISError``,
so callers can catch one class for "anything the client raised."
Subclasses give finer-grained handling where it matters.
"""

from __future__ import annotations

from typing import Any


class GratisGISError(Exception):
    """Base class for every error raised by this package."""


class PortalError(GratisGISError):
    """The portal returned a response that the client could not turn
    into a typed result.

    Wraps the HTTP status, response body (when available), and any
    portal-supplied error code. Use the subclasses below for the
    common cases.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.code = code

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"message={self.args[0]!r}, status={self.status!r}, code={self.code!r})"
        )


class AuthError(PortalError):
    """Authentication or authorization failed.

    Covers token acquisition, token refresh, 401, 403, and PKCE flow
    failures. Callers should usually surface this as "sign in again"
    rather than retry silently.
    """


class NotFoundError(PortalError):
    """The portal returned 404 for the requested resource."""


class ConflictError(PortalError):
    """A write conflicted with a more recent observation on the server.

    Raised on observation commits when the server detects that another
    observation has landed for one of the touched entities since the
    client's ``observed_at`` cursor. The ``body`` attribute carries
    the conflicting observations so the caller can present a diff.
    """


class ValidationError(PortalError):
    """The portal rejected the request because the payload failed
    server-side validation (4xx other than 401/403/404/409).

    The ``body`` attribute typically carries field-level error detail.
    """
