# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin-side test fixtures.

The CI runner ships no QGIS bindings, so plugin modules that need a
name from the ``qgis`` namespace are exercised against stub modules
installed into ``sys.modules`` for the duration of one test. The
production modules import qgis lazily (inside functions) exactly so
this works: importing the module under test is always pure, and the
stub only has to exist by the time the qgis-touching code path runs.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

# What the profile_factory fixture returns: keyword overrides in, a
# real ConnectionProfile out. ``Any`` because the class can only be
# imported once the qgis stub is installed.
ProfileFactory = Callable[..., Any]


def install_qgis_stub(
    monkeypatch: pytest.MonkeyPatch,
    modules: dict[str, dict[str, object]],
) -> dict[str, types.ModuleType]:
    """Install stub modules under the ``qgis`` namespace.

    ``modules`` maps dotted module names (e.g. ``"qgis.core"``) to
    the attributes each should expose. Missing ancestors are created
    empty and children are bound as attributes on their parents, so
    both ``import qgis.core`` and ``from qgis.core import X`` resolve.
    Everything goes through monkeypatch, so the real (absent) qgis
    never leaks between tests.
    """
    registry: dict[str, types.ModuleType] = {}

    def ensure(name: str) -> types.ModuleType:
        if name in registry:
            return registry[name]
        mod = types.ModuleType(name)
        registry[name] = mod
        if "." in name:
            parent_name, _, child = name.rpartition(".")
            setattr(ensure(parent_name), child, mod)
        return mod

    for dotted, attrs in modules.items():
        mod = ensure(dotted)
        for key, value in attrs.items():
            setattr(mod, key, value)
    for name, mod in registry.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return registry


@pytest.fixture
def profile_factory(monkeypatch: pytest.MonkeyPatch) -> ProfileFactory:
    """Factory for real ``ConnectionProfile`` instances without QGIS.

    ``gratisgis_qgis.settings`` imports ``QSettings`` at module level,
    so a stub must be present the first time the module loads; after
    that the cached module keeps working. Using the real dataclass
    (instead of a hand-rolled fake) keeps these tests honest about
    the profile fields the code under test reads.
    """
    install_qgis_stub(
        monkeypatch,
        {"qgis.PyQt.QtCore": {"QSettings": type("QSettings", (), {})}},
    )
    from gratisgis_qgis.settings import ConnectionProfile

    def make(**overrides: object) -> ConnectionProfile:
        base: dict[str, object] = {
            "name": "demo",
            "portal_url": "https://portal.example",
            "verify_tls": True,
            "authcfg_id": "a1b2c3d",
            "user_id": "",
            "api_key_id": "",
            "layer_authcfg_id": "",
            "portal_name": "Demo portal",
            "portal_version": "0.9.25",
            "api_base_url": "https://portal.example/api",
            "oidc_issuer": "https://portal.example/realms/gratis-gis",
            "discovered_at": 123.0,
        }
        base.update(overrides)
        return ConnectionProfile(**base)  # type: ignore[arg-type]

    return make
