# SPDX-License-Identifier: AGPL-3.0-or-later
"""Token storage: where ``TokenSet`` instances are persisted.

This is a Protocol so the QGIS plugin can plug in a backend that
writes to the QGIS auth manager (encrypted at rest, integrated with
QGIS's master password), while CLI scripts and notebooks can use
the in-memory default or roll their own (file-on-disk, OS keyring).
"""

from __future__ import annotations

from typing import Protocol

from gratisgis_client.auth.tokens import TokenSet


class TokenStorage(Protocol):
    """Where the client persists the active token set for a portal.

    Implementations must be safe to call from multiple coroutines on
    the same event loop. Cross-process or cross-thread sharing is
    not a requirement of the protocol; implementations that want to
    support it can add their own locks.
    """

    async def load(self) -> TokenSet | None:
        """Return the stored tokens, or ``None`` if nothing is stored."""

    async def save(self, tokens: TokenSet) -> None:
        """Persist ``tokens``, replacing anything previously stored."""

    async def clear(self) -> None:
        """Remove any stored tokens. Idempotent."""


class InMemoryTokenStorage:
    """Default ``TokenStorage`` implementation. Process-local only.

    Useful for CLI scripts that re-authenticate every run, and for
    unit tests. The QGIS plugin replaces this with a QGIS-auth-manager
    backed implementation in ``gratisgis_qgis.auth_bridge``.
    """

    def __init__(self, initial: TokenSet | None = None) -> None:
        self._tokens = initial

    async def load(self) -> TokenSet | None:
        return self._tokens

    async def save(self, tokens: TokenSet) -> None:
        self._tokens = tokens

    async def clear(self) -> None:
        self._tokens = None
