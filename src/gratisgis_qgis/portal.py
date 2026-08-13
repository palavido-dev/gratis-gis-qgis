# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared portal client access for the plugin.

One ``GratisGISClient`` per connection profile, cached for the life
of the plugin instead of rebuilt per call. The cache matters for
correctness, not just speed: the client's ``AuthManager`` serializes
token refresh on a lock, so every thread (Browser worker, QgsTask
workers, the GUI thread) sharing one client also shares one refresh
instead of racing Keycloak with concurrent refresh_token grants,
which rotates the token out from under the losers.

``list_items`` / ``get_item`` keep the forgiving semantics the
Browser tree has always relied on: an unconfigured or signed-out
profile yields an empty result rather than an exception, because the
tree must render a useful row for fresh profiles, not a stack trace.

This module imports no qgis names at module level (the profile type
is annotation-only), so the pure-Python test suite can exercise the
cache and fetch semantics without QGIS bindings.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from gratisgis_client.client import GratisGISClient
from gratisgis_client.models.item import ItemSummary, ItemType

from .auth_bridge import make_token_storage
from .log import get_logger

if TYPE_CHECKING:
    from .settings import ConnectionProfile

_log = get_logger(__name__)

# Cache key is (authcfg id, portal URL): the authcfg id is the one
# identifier that survives for the life of a signed-in profile (it is
# minted once and reused across re-sign-ins), and the portal URL
# guards against a profile being repointed at a different host while
# keeping its authcfg. Everything else that can change on a profile
# (verify_tls, rediscovered issuer) is handled by the connection
# dialog calling invalidate() on every profile edit, sign-in, and
# sign-out, which are the only places profiles change.
_lock = threading.Lock()
_clients: dict[tuple[str, str], GratisGISClient] = {}


def get_client(profile: ConnectionProfile) -> GratisGISClient:
    """Return the shared client for this profile, building it once.

    Raises ``ValueError`` (from ``to_portal_config``) when the profile
    has not been discovered yet; callers that cannot guarantee
    discovery should gate on ``profile.is_discovered`` first, the way
    ``list_items`` / ``get_item`` below do.
    """
    key = (profile.authcfg_id, profile.portal_url)
    with _lock:
        client = _clients.get(key)
        if client is None:
            config = profile.to_portal_config()
            storage = make_token_storage(profile.authcfg_id)
            client = GratisGISClient(config, token_storage=storage)
            _clients[key] = client
        return client


def invalidate(profile_or_id: ConnectionProfile | str) -> None:
    """Drop cached clients for a profile (or a bare authcfg id).

    Call on sign-out, profile deletion, and after any profile edit or
    re-discovery, BEFORE the change lands: a cached client holds the
    old config and token storage, and reusing it after the profile
    changed is exactly the stale-connection bug the cache could
    otherwise introduce.
    """
    ident = profile_or_id if isinstance(profile_or_id, str) else profile_or_id.authcfg_id
    with _lock:
        for key in [k for k in _clients if k[0] == ident]:
            client = _clients.pop(key)
            client.close()


def list_items(
    profile: ConnectionProfile,
    *,
    types: list[ItemType] | None = None,
    query: str | None = None,
    owner_id: str | None = None,
    limit: int = 200,
) -> list[ItemSummary]:
    """Items the signed-in user can see for this connection.

    Empty list when the profile is not discovered or not signed in;
    callers should not blow up the Browser tree on a fresh profile.
    Network and auth failures still raise so the tree / dock can
    render a per-surface error row.
    """
    if not profile.is_discovered or not profile.authcfg_id:
        _log.debug("list_items: profile not discovered or not signed in: %s", profile.name)
        return []
    out = get_client(profile).items.list(
        types=types,
        query=query,
        owner_id=owner_id,
        limit=limit,
    )
    return list(out.items)


def get_item(profile: ConnectionProfile, item_id: str) -> dict[str, Any] | None:
    """Fetch a single item's full envelope as the wire-shaped dict.

    ``None`` on error rather than raising, because the Browser tree
    paths that call this must degrade to a fallback row on a missing
    or forbidden item, not crash the tree.
    """
    if not profile.is_discovered or not profile.authcfg_id:
        return None
    try:
        return get_client(profile).items.get(item_id).to_api_dict()
    except Exception:
        _log.exception("get_item failed for %s", item_id)
        return None
