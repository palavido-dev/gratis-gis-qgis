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

from gratisgis_client.auth.storage import InMemoryTokenStorage, TokenStorage
from gratisgis_client.auth.tokens import TokenSet

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

    def load(self) -> TokenSet | None:
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

    def save(self, tokens: TokenSet) -> None:
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

    def clear(self) -> None:
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


# -----------------------------------------------------------
# API Header auth configs (private layer rendering)
#
# The token storage above hides an OIDC TokenSet inside an authcfg
# that QGIS itself never interprets. The helpers below manage a
# SECOND kind of authcfg per connection: one QGIS's network stack
# actively applies. The core "API Header" auth method reads its
# config map as header-name -> header-value and sets each entry as a
# raw HTTP header on every request that carries the authcfg, which
# is how a layer URI's `authcfg=` gets `Authorization: Bearer
# ggk_...` onto tile fetches.
# -----------------------------------------------------------

# QgsAuthApiHeaderMethod::AUTH_METHOD_KEY in QGIS core (3.22+).
_API_HEADER_METHOD_KEY = "APIHeader"


def find_api_header_method() -> str | None:
    """The runtime key of QGIS's core API Header auth method, or None.

    Probed against the live auth manager rather than assumed: the
    method ships with QGIS 3.22+ but stripped builds can omit auth
    method plugins, and this probe is the difference between a clear
    "private layers will not render" warning and an authcfg that
    silently does nothing. Exact key first, then a case- and
    punctuation-insensitive match as a hedge against a future rename.
    Returns None without QGIS too, so callers degrade uniformly to
    public-only rendering.
    """
    try:
        from qgis.core import QgsApplication  # type: ignore[import-not-found]

        keys = [str(k) for k in QgsApplication.authManager().authMethodsKeys()]
    except Exception:
        _log.debug("API Header probe: no QGIS auth manager available", exc_info=True)
        return None
    if _API_HEADER_METHOD_KEY in keys:
        return _API_HEADER_METHOD_KEY
    for key in keys:
        if _normalize_method_key(key) == "apiheader":
            return key
    _log.info(
        "API Header auth method not available; auth methods present: %s", keys
    )
    return None


def _normalize_method_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def store_api_header_authcfg(
    authcfg_id: str,
    *,
    name: str,
    method_key: str,
    headers: dict[str, str],
) -> bool:
    """Create or update an API Header authcfg. False on any failure.

    ``headers`` is the method's config map verbatim (header-name ->
    header-value). Overwrite semantics on purpose: re-sign-ins reuse
    the connection's authcfg id so existing layer URIs referencing it
    pick up the fresh key without being rebuilt.
    """
    try:
        from qgis.core import (  # type: ignore[import-not-found]
            QgsApplication,
            QgsAuthMethodConfig,
        )

        cfg = QgsAuthMethodConfig()
        cfg.setId(authcfg_id)
        cfg.setName(name)
        cfg.setMethod(method_key)
        for header, value in headers.items():
            cfg.setConfig(header, value)
        return bool(QgsApplication.authManager().storeAuthenticationConfig(cfg, True))
    except Exception:
        _log.exception("Failed to store API Header authcfg %s", authcfg_id)
        return False


def remove_authcfg(authcfg_id: str) -> None:
    """Best-effort removal of one auth-manager entry.

    Never raises: every caller is on a teardown path (sign-out,
    profile delete, degrade-to-public) that must complete locally
    whatever the auth database thinks. An empty id is a no-op.
    """
    if not authcfg_id:
        return
    try:
        from qgis.core import QgsApplication  # type: ignore[import-not-found]

        QgsApplication.authManager().removeAuthenticationConfig(authcfg_id)
    except Exception:
        _log.exception("Failed to remove authcfg %s", authcfg_id)
