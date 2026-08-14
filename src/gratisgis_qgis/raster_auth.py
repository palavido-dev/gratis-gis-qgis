# SPDX-License-Identifier: AGPL-3.0-or-later
"""Teach GDAL how to authenticate to one portal's tile_layer files.

tile_layer items are rasters served from an authenticated endpoint and
read by GDAL over ``/vsicurl``. QGIS's own auth system cannot help
here: auth methods are applied to ``QNetworkRequest``, and ``/vsicurl``
issues its requests through libcurl inside GDAL, so an ``authcfg=`` on
the URI is silently ignored (verified against QGIS 4.0.2: the layer is
simply invalid). GDAL takes its credentials from configuration options
instead.

The option is registered against the portal's tile_layer path prefix
rather than globally, so the header is only ever sent to the portal
that issued it. That matters: the value is a real portal API key, and a
global ``GDAL_HTTP_HEADERS`` would attach it to every HTTP raster the
user opens, including other people's servers.
"""
from __future__ import annotations

from .auth_bridge import read_api_header
from .browser.uris import tile_layer_file_root
from .log import get_logger
from .settings import ConnectionProfile

_log = get_logger(__name__)

# Prefixes already configured this session, so repeated Browser
# refreshes do not re-read the auth database (which can prompt for the
# master password) on every expand.
_configured: set[str] = set()


def configure_gdal_auth(profile: ConnectionProfile, *, force: bool = False) -> bool:
    """Register the profile's layer key as a GDAL HTTP header.

    Returns True when a header is in place for this portal. False means
    raster tile_layers will only work for public items, which is the
    correct degraded behaviour rather than a hard failure: a user who
    never signed in, or who dismissed the master-password prompt, can
    still browse and open public data.

    ``force`` re-reads after a sign-in or sign-out, where the key
    changed underneath us.
    """
    prefix = f"/vsicurl/{tile_layer_file_root(profile.portal_url)}"
    if not force and prefix in _configured:
        return True

    header = read_api_header(profile.layer_authcfg_id)
    if header is None:
        # Nothing to install. Drop any previous registration for this
        # prefix so a signed-out profile stops sending a stale key.
        if prefix in _configured:
            _set_option(prefix, None)
            _configured.discard(prefix)
        return False

    name, value = header
    if not _set_option(prefix, f"{name}: {value}"):
        return False
    _configured.add(prefix)
    _log.debug("GDAL auth header registered for %s", prefix)
    return True


def forget(profile: ConnectionProfile) -> None:
    """Clear the header for this portal (sign-out, profile delete)."""
    prefix = f"/vsicurl/{tile_layer_file_root(profile.portal_url)}"
    _set_option(prefix, None)
    _configured.discard(prefix)


def _set_option(prefix: str, value: str | None) -> bool:
    """Set or clear the path-scoped GDAL header option.

    Path-specific options need GDAL 3.6+. On anything older we do NOT
    fall back to the global option: that would leak the portal's API
    key to every other HTTP source the user opens, which is a worse
    outcome than raster tile_layers not loading.
    """
    try:
        from osgeo import gdal  # type: ignore[import-not-found]
    except ImportError:
        _log.debug("osgeo.gdal unavailable; skipping raster auth setup")
        return False

    setter = getattr(gdal, "SetPathSpecificOption", None)
    if setter is None:
        _log.warning(
            "This GDAL has no path-specific options, so the portal key cannot be "
            "scoped to one host; private raster tile layers will not load."
        )
        return False
    try:
        setter(prefix, "GDAL_HTTP_HEADERS", value)
        return True
    except Exception:
        _log.exception("Could not configure GDAL auth for %s", prefix)
        return False
