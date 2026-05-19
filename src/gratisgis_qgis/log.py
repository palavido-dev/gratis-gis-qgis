# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin file logging.

QGIS has its own log surfaces (the Log Messages Panel, the message
bar), but for the plugin we also want a rolling file log so users
can attach logs to GitHub issues without screenshotting the panel.
The file lives next to the plugin's local cache in
``AppDataLocation/gratisgis/`` and rotates at 5 MB with 3 backups.

Use ``get_logger(__name__)`` from anywhere in ``gratisgis_qgis`` to
log; the first call initializes the file handler. The QGIS log
panel handler is added in parallel so messages also show up there
without separate plumbing.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_INITIALIZED = False
_LOG_DIR_OVERRIDE: Path | None = None


def set_log_dir(path: Path) -> None:
    """Override the log directory (tests use this; production reads
    from QStandardPaths). Must be called before the first
    ``get_logger`` call to take effect.
    """
    global _LOG_DIR_OVERRIDE
    _LOG_DIR_OVERRIDE = path


def _default_log_dir() -> Path:
    if _LOG_DIR_OVERRIDE is not None:
        return _LOG_DIR_OVERRIDE
    # Defer the PyQt import so the module loads outside QGIS for tests.
    try:
        from qgis.PyQt.QtCore import QStandardPaths  # type: ignore[import-not-found]

        base = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
    except ImportError:
        base = Path.home() / ".gratisgis"
    return base / "gratisgis" / "logs"


def _init_root_logger() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    logger = logging.getLogger("gratisgis_qgis")
    logger.setLevel(logging.DEBUG)
    log_dir = _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "plugin.log"
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    # Also pipe to the QGIS log panel when running inside QGIS.
    try:
        from gratisgis_qgis.log_qgis_handler import QGISLogPanelHandler

        logger.addHandler(QGISLogPanelHandler())
    except ImportError:
        pass
    logger.propagate = False
    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger.

    ``name`` follows the usual ``__name__`` pattern; the file handler
    is shared across all loggers under ``gratisgis_qgis``.
    """
    _init_root_logger()
    return logging.getLogger(name)
