# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging handler that forwards to QGIS's Log Messages Panel.

Kept in its own module so the file logger can fall back gracefully
when running outside QGIS. Everything QGIS-touching is deferred to
construction time: this module is imported during logger init, which
runs on the plugin's very first import, and a module-level qgis
import or enum access that raised there would break PLUGIN LOAD
outright. Construction raises (``ImportError`` without the bindings,
``AttributeError`` when the message-level enum resolves nowhere) and
the file logger catches broadly, degrading to file-only logging with
no QGIS mirroring instead of a dead plugin.
"""

from __future__ import annotations

import logging

from .qgis_compat import resolve_enum


class QGISLogPanelHandler(logging.Handler):
    """Forward records to QGIS's QgsMessageLog under the "GratisGIS" tag."""

    def __init__(self) -> None:
        super().__init__()
        from qgis.core import Qgis, QgsMessageLog  # type: ignore[import-not-found]

        self._message_log = QgsMessageLog
        # Qgis.Info / Warning / Critical were class-level shortcuts on
        # QGIS 3; the scoped Qgis.MessageLevel enum is their home on
        # QGIS 3.22+ and the only one under QGIS 4's strict PyQt6.
        scoped = getattr(Qgis, "MessageLevel", None)
        info = resolve_enum((scoped, "Info"), (Qgis, "Info"))
        warning = resolve_enum((scoped, "Warning"), (Qgis, "Warning"))
        critical = resolve_enum((scoped, "Critical"), (Qgis, "Critical"))
        self._default_level = info
        self._level_map: dict[int, object] = {
            logging.DEBUG: info,
            logging.INFO: info,
            logging.WARNING: warning,
            logging.ERROR: critical,
            logging.CRITICAL: critical,
        }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = self._level_map.get(record.levelno, self._default_level)
            self._message_log.logMessage(self.format(record), "GratisGIS", level)
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)
