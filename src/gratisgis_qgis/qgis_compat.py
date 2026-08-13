# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-version QGIS / Qt enum resolution.

Most Qt enum drift (Qt 5 unscoped vs Qt 6 scoped) is handled inline
by writing the scoped form (``Qt.ItemFlag.ItemIsSelectable``), which
works on both PyQt5 and PyQt6. QGIS's own enums are messier: values
kept moving from class-level shortcuts (``QgsDataItem.Fertile``,
the layer-type and writer-error attributes on ``QgsLayerItem`` and
``QgsVectorFileWriter``) into scoped enums
(``Qgis.BrowserItemCapability``, ``Qgis.BrowserLayerType``,
``QgsVectorFileWriter.WriterError``), and QGIS 4 under strict PyQt6
drops the old shortcuts entirely. ``resolve_enum`` lets each call
site declare its candidates newest-first and fail loudly, in one
place, when none resolve.

This module imports nothing from ``qgis`` so the stubbed test suite
(and any module that must stay importable outside QGIS) can use it
freely; callers pass in whatever holder objects they have.
"""
from __future__ import annotations


def resolve_enum(*candidates: tuple[object, str]) -> object:
    """Try each (holder, attribute_name) pair until one resolves.

    ``None`` holders are skipped, so callers can inline
    ``getattr(Qgis, "SomeEnum", None)`` for scoped homes that only
    exist on newer QGIS. Raises ``AttributeError`` listing every
    attempted path if none match, so a future Qt / QGIS shuffle gives
    a clean error pointing at the resolver instead of a per-call-site
    mystery.
    """
    tried: list[str] = []
    for holder, attr in candidates:
        if holder is None:
            continue
        tried.append(f"{getattr(holder, '__name__', holder)}.{attr}")
        if hasattr(holder, attr):
            return getattr(holder, attr)
    raise AttributeError(
        f"None of these resolve to a usable enum value: {', '.join(tried)}"
    )
