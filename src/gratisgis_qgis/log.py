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


def _default_log_dir() -> Path:
    # Defer the PyQt import so the module loads outside QGIS for tests.
    try:
        from qgis.PyQt.QtCore import QStandardPaths  # type: ignore[import-not-found]

        # Use the scoped enum form because PyQt6 (Qt 6 / QGIS 4)
        # dropped the unscoped class-level shortcuts. The scoped
        # form ALSO works on PyQt5 (Qt 5 / QGIS 3.34), so one
        # spelling covers both.
        app_data = QStandardPaths.StandardLocation.AppDataLocation
        base = Path(QStandardPaths.writableLocation(app_data))
    except ImportError:
        base = Path.home() / ".gratisgis"
    return base / "gratisgis" / "logs"


def log_directory() -> Path:
    """The directory the plugin writes logs into.

    Public because the log file is no longer the only thing written
    there: a freeze dump is a separate file (it has to be written
    without going through the logging stack, see
    ``freeze_watch.write_dump``) and belongs beside the log so a user
    attaching diagnostics to an issue finds both in one place.
    """
    return _default_log_dir()


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
    # Also pipe to the QGIS log panel when running inside QGIS. Catch
    # broadly, not just ImportError: this runs on the plugin's first
    # import, and the handler's constructor can also fail on a QGIS /
    # Qt build whose message-level enums moved (AttributeError). Any
    # such failure must degrade to file-only logging with no QGIS
    # mirroring; raising here would break plugin load outright.
    try:
        from .log_qgis_handler import QGISLogPanelHandler

        logger.addHandler(QGISLogPanelHandler())
    except Exception:
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


def teardown_logging() -> None:
    """Detach and close every handler on the plugin's root logger.

    Called from the plugin's ``unload`` hook. The stdlib logging
    registry outlives a plugin reload (QGIS purges the plugin's
    modules from ``sys.modules``, but ``logging`` keeps its logger
    objects), while the ``_INITIALIZED`` guard dies with this
    module. Without an explicit teardown every reload stacks one
    more file handler onto the persistent logger: duplicated lines
    in plugin.log and, on Windows, an open handle that keeps the
    file locked across plugin upgrades. Resetting ``_INITIALIZED``
    lets the next ``get_logger`` call rebuild handlers from scratch.
    """
    global _INITIALIZED
    logger = logging.getLogger("gratisgis_qgis")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    _INITIALIZED = False
