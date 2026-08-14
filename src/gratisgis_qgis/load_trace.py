# SPDX-License-Identifier: AGPL-3.0-or-later
"""Leave a trail through project load, so a freeze names a layer.

Paired with ``freeze_watch``. That module says the GUI thread stopped
and dumps stacks; this one says what it stopped on. Neither is much
use alone: a stack full of Qt frames does not identify which of eleven
portal layers was being resolved, and a layer name without a stack
does not say what it was waiting for.

The trail is one line per layer as it joins the project, plus a line
when a project read begins. A layer that hangs during construction
never reaches ``layerWasAdded``, so the culprit is not the last layer
logged, it is the one after it. That is a real limit of hooking a
signal QGIS emits once the layer already exists, and it is still
enough: the last logged line plus the project's own layer order names
a suspect, where silence named nothing.

One line is also recorded that has nothing to do with any layer:
whether the QGIS auth database is unlocked at the moment the read
starts. The freeze under investigation is believed to need a locked
auth database plus layers carrying ``authcfg=``, and that flag is the
cheap half of the pair. It is read with ``masterPasswordIsSet()``,
which reports what is already in memory and does not itself prompt.
Asking the auth manager a question that could raise the very prompt
being investigated would be its own bug.

Nothing here is allowed to raise. It runs inside project load, in a
signal handler, for every layer including layers with no connection to
the portal. A diagnostic that breaks the thing it observes is worse
than no diagnostic.
"""
from __future__ import annotations

import contextlib
import re
from typing import Any

from .browser.uris import parse_portal_layer_source, parse_tile_layer_uri
from .log import get_logger

_log = get_logger(__name__)

#: Matches the authcfg id in a layer URI, in both spellings QGIS uses.
_AUTHCFG_RE = re.compile(r"authcfg=['\"]?([A-Za-z0-9_-]+)['\"]?")

#: How much of a source string is safe to log.
#:
#: Sources are long and mostly noise, but they are also the one place
#: a credential can appear: a portal API key reaches GDAL through a
#: header rather than a URI, yet nothing stops a future builder from
#: putting one in a query string. Truncating bounds the blast radius of
#: that mistake without needing to predict it.
_SOURCE_PREVIEW = 120


def authcfg_id_in(source: str) -> str:
    """The authcfg id referenced by a layer source, or "".

    The id itself is not a secret: it is a random handle, stored in
    plain text in every project file that uses the layer, and useless
    without the auth database it points into. Logging it is what lets a
    dangling reference be recognised as dangling.
    """
    if not source:
        return ""
    match = _AUTHCFG_RE.search(source)
    return match.group(1) if match else ""


def describe_layer(name: str, source: str) -> str:
    """One log line's worth of description for a layer being added.

    Says whether the layer is ours, since this fires for every layer in
    the project and most of them will not be, and whether it carries an
    authcfg, since that is the suspected trigger.
    """
    portal = "portal" if _is_portal_source(source) else "other"
    authcfg = authcfg_id_in(source)
    auth_note = f", authcfg={authcfg}" if authcfg else ", no authcfg"
    preview = source[:_SOURCE_PREVIEW]
    if len(source) > _SOURCE_PREVIEW:
        preview = f"{preview}..."
    return f"{name!r} ({portal}{auth_note}) {preview}"


def _is_portal_source(source: str) -> bool:
    """True when this source came out of one of our URI builders."""
    if not source:
        return False
    with contextlib.suppress(Exception):
        if parse_portal_layer_source(source) is not None:
            return True
    with contextlib.suppress(Exception):
        if parse_tile_layer_uri(source) is not None:
            return True
    return False


def auth_db_state() -> str:
    """Whether the auth database is unlocked, in words, without asking.

    ``masterPasswordIsSet()`` reports the in-memory state and does not
    prompt. "locked" here does not mean a password is definitely
    configured, only that this session has not supplied one; combined
    with a layer carrying an authcfg, that is the state where QGIS goes
    looking for the user.
    """
    try:
        from qgis.core import QgsApplication  # type: ignore[import-not-found]

        if QgsApplication.authManager().masterPasswordIsSet():
            return "unlocked"
        return "locked (QGIS will prompt if a layer needs it)"
    except Exception:
        return "unknown"


class LoadTracer:
    """Logs project reads and layer additions for the session.

    Held by the plugin and disconnected on unload, for the same reason
    ``ExtentApplier`` is: a reload would otherwise leave this instance's
    slot bound to a module the reload has replaced.
    """

    def __init__(self) -> None:
        self._connected = False

    def install(self) -> None:
        from qgis.core import QgsProject  # type: ignore[import-not-found]

        if self._connected:
            return
        project = QgsProject.instance()
        # Connected individually: a QGIS build missing one of these
        # signals should cost that one line, not the whole trail.
        with contextlib.suppress(Exception):
            project.readProject.connect(self._on_read_project)
        with contextlib.suppress(Exception):
            project.layerWasAdded.connect(self._on_layer_added)
        self._connected = True

    def remove(self) -> None:
        from qgis.core import QgsProject  # type: ignore[import-not-found]

        if not self._connected:
            return
        project = QgsProject.instance()
        with contextlib.suppress(TypeError, RuntimeError, Exception):
            project.readProject.disconnect(self._on_read_project)
        with contextlib.suppress(TypeError, RuntimeError, Exception):
            project.layerWasAdded.disconnect(self._on_layer_added)
        self._connected = False

    def _on_read_project(self, _doc: Any = None) -> None:
        # Suppressed rather than handled: this is a signal handler
        # inside project load, and a diagnostic that can abort the load
        # it is describing is worse than a missing log line.
        with contextlib.suppress(Exception):
            _log.info("project read starting; auth database is %s", auth_db_state())

    def _on_layer_added(self, layer: Any) -> None:
        try:
            name = layer.name()
            source = layer.source()
            if not isinstance(source, str):
                source = ""
            _log.info("layer added: %s", describe_layer(str(name), source))
        except Exception:  # pragma: no cover - defensive
            pass
