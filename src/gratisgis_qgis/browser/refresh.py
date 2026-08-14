# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tell the Browser panel that a connection's auth state changed.

Signing in or out changes what a connection should show, but nothing
in QGIS notices on its own: the Browser tree only rebuilds when the
user asks it to. Before this existed, signing out left the private
layers sitting in the tree until the user thought to hit refresh, and
the connection manager is a separate dialog, so there was no reason
for them to think of it.

This is a convenience, not the fix. The reason the stale rows were
served in the first place is that the tree nodes cached a
``ConnectionProfile`` from when they were built; that is fixed in
``items.py``, and a node now reports the current state whenever QGIS
gets round to expanding it. Refreshing just means the user does not
have to ask.

Targeted at our own subtree rather than reloading the whole model.
A full reload re-enumerates every provider in the panel, including
filesystem and database connections that have nothing to do with us
and can be slow to walk.
"""
from __future__ import annotations

from ..log import get_logger

_log = get_logger(__name__)

#: Path of the plugin's root node, as ``items.RootItem`` sets it.
#: Kept in step by ``tests/plugin/test_browser_refresh.py``, because a
#: silent mismatch here degrades to "the user refreshes by hand", which
#: is precisely the old behaviour and would go unnoticed.
ROOT_PATH = "gratisgis:/"


def refresh_browser_tree() -> bool:
    """Rebuild the GratisGIS subtree in the Browser panel.

    Returns True when a refresh was requested. False covers every
    ordinary reason it could not be: no QGIS interface (tests, headless
    scripts), no browser model yet, or a QGIS build whose model does not
    offer a refresh entry point.

    Never raises. Every caller is finishing a sign-in, sign-out, or
    delete that has already succeeded, and failing to redraw a tree must
    not turn a completed action into an error dialog.
    """
    try:
        from qgis.utils import iface  # type: ignore[import-not-found]
    except Exception:
        _log.debug("browser refresh: no qgis.utils.iface")
        return False
    if iface is None:
        return False
    try:
        model = iface.browserModel()
    except Exception:
        _log.debug("browser refresh: no browser model", exc_info=True)
        return False
    if model is None:
        return False

    # refresh(path) rebuilds one subtree; reload() is the whole panel.
    # Prefer the narrow one and only fall back if this build has no
    # such method, which is the sort of thing that changes between
    # major versions without warning.
    refresh = getattr(model, "refresh", None)
    if callable(refresh):
        try:
            refresh(ROOT_PATH)
            _log.debug("browser refresh: refreshed %s", ROOT_PATH)
            return True
        except Exception:
            _log.debug("browser refresh: refresh(path) failed", exc_info=True)

    reload_all = getattr(model, "reload", None)
    if callable(reload_all):
        try:
            reload_all()
            _log.debug("browser refresh: reloaded the whole model")
            return True
        except Exception:
            _log.debug("browser refresh: reload() failed", exc_info=True)
    return False
