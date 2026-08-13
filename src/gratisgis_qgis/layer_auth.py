# SPDX-License-Identifier: AGPL-3.0-or-later
"""Portal API key lifecycle for private layer rendering.

Why an API key at all: layers on the canvas fetch their tiles
through QGIS's own network stack, which knows nothing about the
plugin's OIDC session, so every non-public layer used to render
empty. A per-connection READ-ONLY portal key, stored in a QGIS "API
Header" authcfg and referenced from layer URIs via ``authcfg=``, is
the mechanism that lets the stock providers send ``Authorization:
Bearer ggk_...`` on those fetches. Read-only on purpose: a layer URI
leaks easily (project files, screenshots, pasted debug output) and
the portal refuses read-only keys on any method outside
GET/HEAD/OPTIONS, so a leaked URI cannot write. Edits keep using the
OIDC session.

QGIS-free on purpose. The authcfg half lives in ``auth_bridge`` and
the orchestration in the connection dialog; this module owns naming,
minting, and revocation so tests can pin the exact requests the
sign-in and sign-out flows send at the client's transport seam.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .log import get_logger

if TYPE_CHECKING:
    from gratisgis_client.client import GratisGISClient
    from gratisgis_client.endpoints.api_keys import ApiKeyCreated

_log = get_logger(__name__)

LAYER_KEY_EXPIRES_DAYS = 365
"""Safety net for keys whose revoke path never runs (a QGIS profile
wiped or lost while the portal row survives): the key self-expires.
Normal flows revoke on sign-out and re-mint on every sign-in long
before this, and an expired key simply stops rendering private
layers until the next sign-in mints a fresh one."""


def layer_key_name(profile_name: str) -> str:
    """The portal-visible name for a connection's layer key.

    Users see this in the portal's API keys list; carrying the QGIS
    connection name makes it obvious which client minted it and safe
    to revoke from the portal side.
    """
    return f"QGIS layers ({profile_name})"


def mint_layer_key(client: GratisGISClient, profile_name: str) -> ApiKeyCreated:
    """Mint the read-only key sign-in stores into the layer authcfg.

    Raises on failure; the caller degrades to public-only rendering
    with a warning rather than failing the sign-in.
    """
    return client.api_keys.create(
        name=layer_key_name(profile_name),
        read_only=True,
        expires_in_days=LAYER_KEY_EXPIRES_DAYS,
    )


def revoke_layer_key(client: GratisGISClient, key_id: str) -> bool:
    """Best-effort server-side revocation of a layer key.

    Returns False (and logs) instead of raising: every caller sits on
    a teardown path (sign-out, profile delete, pre-mint cleanup on
    re-sign-in) that must proceed regardless, and the expiry on the
    key bounds the damage of a failed revoke. An empty id is a no-op.
    """
    if not key_id:
        return False
    try:
        client.api_keys.revoke(key_id)
    except Exception:
        _log.warning(
            "Failed to revoke layer API key %s (continuing)", key_id, exc_info=True
        )
        return False
    return True
