# SPDX-License-Identifier: AGPL-3.0-or-later
"""GratisGIS QGIS plugin entry point.

QGIS loads a plugin by importing the package directory and calling
``classFactory(iface)``, which must return the plugin instance.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _purge_client_modules() -> None:
    """Drop every ``gratisgis_client`` entry from ``sys.modules``."""
    for key in [
        k
        for k in sys.modules
        if k == "gratisgis_client" or k.startswith("gratisgis_client.")
    ]:
        del sys.modules[key]


def _install_vendored_client() -> None:
    """Alias the vendored ``gratisgis_client`` to its canonical name.

    Zip builds (scripts/make_zip.py) ship the client library under
    ``_vendor/gratisgis_client`` so the plugin runs on a stock QGIS
    install with no pip step. The client's modules import each other
    absolutely (``from gratisgis_client.x import ...``), so the
    vendored copy only resolves if ``sys.modules["gratisgis_client"]``
    points at it before anything imports the client. Registering the
    alias here, at package import time, lets every
    ``gratisgis_client`` import (the plugin's and the vendored
    library's own) reach the vendored tree with no build-time import
    rewriting.

    In a dev checkout there is no ``_vendor`` directory and this is a
    no-op: the pip-installed / src-tree client is used as usual. When
    the vendor tree exists we deliberately prefer it, even over an
    importable pip copy: the zip was tested against exactly this
    vendored version, and a version-skewed pip copy silently winning
    is the failure mode vendoring exists to prevent.
    """
    vendor_name = __name__ + "._vendor.gratisgis_client"
    vendor_dir = Path(__file__).resolve().parent / "_vendor" / "gratisgis_client"
    if not (vendor_dir / "__init__.py").is_file():
        return

    # Drop client modules registered by a previous plugin load: QGIS
    # purges only ``gratisgis_qgis.*`` from sys.modules on unload, so
    # after an in-place plugin upgrade the alias would otherwise keep
    # serving the previous version's modules.
    _purge_client_modules()

    spec = importlib.util.find_spec(vendor_name)
    if spec is None or spec.loader is None:
        raise ImportError(f"Vendored client is not importable: {vendor_name}")
    module = importlib.util.module_from_spec(spec)
    # Register both names BEFORE executing the module body: the
    # package's own __init__ does ``from gratisgis_client.client
    # import ...``, which must already resolve through the alias.
    sys.modules[vendor_name] = module
    sys.modules["gratisgis_client"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Leave no partial client modules behind on a failed import;
        # a retry should start from a clean registry.
        _purge_client_modules()
        sys.modules.pop(vendor_name, None)
        raise
    # exec_module bypasses the part of the import system that binds a
    # submodule as an attribute on its parent package; set it so
    # attribute access on ``gratisgis_qgis._vendor`` behaves normally.
    sys.modules[__name__ + "._vendor"].gratisgis_client = module  # type: ignore[attr-defined]


_install_vendored_client()


def classFactory(iface):  # type: ignore[no-untyped-def]  # QGIS API name
    """Return the plugin instance.

    Lazy-import the actual plugin class so a syntax error or missing
    dependency in ``plugin.py`` doesn't prevent QGIS from at least
    surfacing the load error in its UI.
    """
    from .plugin import GratisGISPlugin

    return GratisGISPlugin(iface)
