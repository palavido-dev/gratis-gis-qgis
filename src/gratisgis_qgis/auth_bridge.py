# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bridge between the portable client's TokenStorage protocol and the
QGIS auth manager.

QGIS's QgsAuthManager encrypts credentials at rest with the user's
master password and is the conventional place for QGIS plugins to
keep secrets. We store each connection's TokenSet as a JSON blob
inside an authcfg entry, keyed by a stable id that the connection
profile carries.

Falls back to an in-memory token store at import time when running
outside QGIS so unit tests can exercise paths that touch this module.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from gratisgis_client.auth.storage import InMemoryTokenStorage, TokenStorage
from gratisgis_client.auth.tokens import TokenSet

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)


class QgisAuthManagerTokenStorage(TokenStorage):
    """TokenStorage implementation backed by QgsAuthManager.

    Each connection profile gets one authcfg entry. The entry's
    ``config`` map carries the token JSON; QGIS encrypts it before
    writing to disk.

    Construction does not validate that the auth manager is
    initialized. Call ``ensure_master_password()`` on the running
    QgsAuthManager before using the storage; the QGIS plugin's
    settings UI handles that.
    """

    AUTH_METHOD = "Basic"  # Any non-None method; we store JSON in 'password'.
    CONFIG_NAME_PREFIX = "GratisGIS "

    def __init__(self, authcfg_id: str) -> None:
        self._authcfg_id = authcfg_id
        from qgis.core import QgsApplication  # type: ignore[import-not-found]

        self._mgr = QgsApplication.authManager()

    async def load(self) -> TokenSet | None:
        from qgis.core import QgsAuthMethodConfig  # type: ignore[import-not-found]

        cfg = QgsAuthMethodConfig()
        ok = self._mgr.loadAuthenticationConfig(self._authcfg_id, cfg, True)
        if not ok or not cfg.isValid():
            return None
        try:
            payload = cfg.config("password")
        except Exception:
            return None
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            _log.warning("Stored token blob for authcfg %s is malformed", self._authcfg_id)
            return None
        return TokenSet(**data)

    async def save(self, tokens: TokenSet) -> None:
        from qgis.core import QgsAuthMethodConfig  # type: ignore[import-not-found]

        cfg = QgsAuthMethodConfig()
        cfg.setId(self._authcfg_id)
        cfg.setName(f"{self.CONFIG_NAME_PREFIX}{self._authcfg_id}")
        cfg.setMethod(self.AUTH_METHOD)
        cfg.setConfig("password", json.dumps(asdict(tokens)))
        # storeAuthenticationConfig with overwrite=True both inserts new and updates existing.
        ok = self._mgr.storeAuthenticationConfig(cfg, True)
        if not ok:
            raise RuntimeError(
                f"QgsAuthManager.storeAuthenticationConfig failed for {self._authcfg_id}"
            )

    async def clear(self) -> None:
        # Returns False when the id doesn't exist; treat that as no-op.
        self._mgr.removeAuthenticationConfig(self._authcfg_id)


def make_token_storage(authcfg_id: str | None) -> TokenStorage:
    """Return the best available TokenStorage.

    Inside QGIS with a valid ``authcfg_id``, returns the QGIS auth
    manager-backed storage. Without QGIS (tests, smoke scripts) or
    without an authcfg id, returns an in-memory storage.
    """
    if not authcfg_id:
        return InMemoryTokenStorage()
    try:
        return QgisAuthManagerTokenStorage(authcfg_id)
    except ImportError:
        return InMemoryTokenStorage()
