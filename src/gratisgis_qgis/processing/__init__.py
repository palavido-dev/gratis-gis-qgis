# SPDX-License-Identifier: AGPL-3.0-or-later
"""GratisGIS Processing provider: portal actions as algorithms.

Processing is how QGIS batches. Wrapping publish and clone as
algorithms is what lets someone publish a folder of layers in batch
mode, or put "clone this layer for the field crew" inside a Model
Designer model, instead of clicking through a dialog once per layer.

The algorithms reuse the same pipelines the dialogs run; nothing here
talks to the portal in a second way.
"""
from typing import Any

__all__ = ["GratisGISProcessingProvider"]


def __getattr__(name: str) -> Any:
    # Lazy on purpose: ``provider`` imports Processing base classes
    # that only exist under real QGIS bindings, while ``support`` is
    # pure and unit-tested. Importing the package must not force the
    # bindings, or the pure half becomes untestable.
    if name == "GratisGISProcessingProvider":
        from .provider import GratisGISProcessingProvider

        return GratisGISProcessingProvider
    raise AttributeError(name)
