# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentication: PKCE flow, token storage, refresh.

Two surfaces matter to callers:

- ``PKCEFlow`` runs the interactive sign-in via a loopback HTTP
  server and the system browser, blocking the calling thread until
  the redirect lands. The QGIS plugin runs it inside a background
  task.

- ``TokenStorage`` is a protocol for persisting tokens. The default
  implementation keeps them in-memory; the QGIS plugin substitutes
  an implementation backed by the QGIS auth manager.

``AuthManager`` ties these together and handles refresh-on-401.
"""

from gratisgis_client.auth.manager import AuthManager
from gratisgis_client.auth.pkce import PKCEChallenge, PKCEFlow
from gratisgis_client.auth.storage import InMemoryTokenStorage, TokenStorage
from gratisgis_client.auth.tokens import TokenSet

__all__ = [
    "AuthManager",
    "InMemoryTokenStorage",
    "PKCEChallenge",
    "PKCEFlow",
    "TokenSet",
    "TokenStorage",
]
