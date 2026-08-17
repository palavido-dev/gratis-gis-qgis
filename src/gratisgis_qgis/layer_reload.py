# SPDX-License-Identifier: AGPL-3.0-or-later
"""Make layers already on the canvas notice that the credential changed.

Signing back in stores a fresh key under the same authcfg id, and
``auth_bridge.forget_cached_authcfg`` makes the auth manager drop its
cached copy. Neither reaches a provider that has already built its
request template: a layer added before the change keeps whatever it
resolved the first time, so after a sign-out and sign-in the tree
behaves and the canvas stays blank until QGIS is restarted.

Reloading the provider is what clears that. It is cheap for a tiled
layer, which holds no features, and it costs a redraw for the ones
actually affected.

Scoped to layers whose source names one of OUR authcfg ids. Reloading
somebody's PostGIS connection or a WMS from another organisation
because they happened to be in the project would be rude and slow.
"""
from __future__ import annotations

from typing import Any

from .browser.uris import uri_param
from .log import get_logger

_log = get_logger(__name__)


def layers_using_authcfg(layers: Any, authcfg_id: str) -> list[Any]:
    """The project layers whose source carries this authcfg id.

    Takes an iterable of layers rather than reading the project, so the
    selection rule can be tested without QGIS.
    """
    if not authcfg_id:
        return []
    out = []
    for layer in layers:
        try:
            source = layer.source()
        except Exception:  # pragma: no cover - defensive
            _log.debug("layer source read failed", exc_info=True)
            continue
        if not isinstance(source, str) or not source:
            continue
        # By name, not by substring: "authcfg=abc1234" must not match a
        # layer whose URL happens to contain the id, and the parameter
        # can sit anywhere in the bag.
        if uri_param(source, "authcfg") == authcfg_id:
            out.append(layer)
    return out


def reload_layers_using(authcfg_id: str) -> int:
    """Reload every canvas layer using this credential. Never raises.

    Returns how many were reloaded. Every caller has just completed a
    sign-in or sign-out; failing to redraw must not turn that into an
    error.
    """
    if not authcfg_id:
        return 0
    try:
        from qgis.core import QgsProject  # type: ignore[import-not-found]

        layers = list(QgsProject.instance().mapLayers().values())
    except Exception:
        _log.debug("no project to reload layers in", exc_info=True)
        return 0

    reloaded = 0
    for layer in layers_using_authcfg(layers, authcfg_id):
        try:
            provider = layer.dataProvider()
            if provider is not None and hasattr(provider, "reloadData"):
                provider.reloadData()
            layer.triggerRepaint()
            reloaded += 1
        except Exception:
            _log.debug(
                "could not reload %s", getattr(layer, "name", lambda: "?")(),
                exc_info=True,
            )
    if reloaded:
        _log.info(
            "reloaded %d layer(s) using authcfg %s after a credential change",
            reloaded,
            authcfg_id,
        )
    return reloaded
