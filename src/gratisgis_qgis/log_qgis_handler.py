# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging handler that forwards to QGIS's Log Messages Panel.

Kept in its own module so the file logger can fall back gracefully
when running outside QGIS (e.g. in unit tests). The handler raises
``ImportError`` at construction time if the QGIS bindings are
unavailable, which the file logger catches.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from qgis.core import Qgis, QgsMessageLog  # type: ignore[import-not-found]


class QGISLogPanelHandler(logging.Handler):
    """Forward records to QGIS's QgsMessageLog under the "GratisGIS" tag."""

    _LEVEL_MAP: ClassVar[dict[int, int]] = {
        logging.DEBUG: Qgis.Info,
        logging.INFO: Qgis.Info,
        logging.WARNING: Qgis.Warning,
        logging.ERROR: Qgis.Critical,
        logging.CRITICAL: Qgis.Critical,
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = self._LEVEL_MAP.get(record.levelno, Qgis.Info)
            QgsMessageLog.logMessage(self.format(record), "GratisGIS", level)
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)
