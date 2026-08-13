# SPDX-License-Identifier: AGPL-3.0-or-later
"""QGIS log-panel handler resolution across enum generations.

This module is imported during logger init, which runs on the
plugin's very first import; a level-enum AttributeError there used
to be a plugin-load breaker on QGIS 4. Pinned: scoped
Qgis.MessageLevel resolves first, the legacy class attributes still
work, and a build with neither degrades to file-only logging
instead of raising out of get_logger.
"""
from __future__ import annotations

import logging
import logging.handlers

import pytest

from tests.plugin.conftest import install_qgis_stub


def _record(level: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="gratisgis_qgis.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=None,
    )


class _RecorderMessageLog:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, object]] = []

    def logMessage(self, text: str, tag: str, level: object) -> None:  # QGIS API name
        self.messages.append((text, tag, level))


class TestLevelResolution:
    def test_scoped_message_level_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _MessageLevel:
            Info = "scoped-info"
            Warning = "scoped-warning"
            Critical = "scoped-critical"

        class _Qgis:
            MessageLevel = _MessageLevel
            # Legacy attrs present too; scoped must win so QGIS 4
            # deprecation shims never get exercised.
            Info = "legacy-info"
            Warning = "legacy-warning"
            Critical = "legacy-critical"

        sink = _RecorderMessageLog()
        install_qgis_stub(
            monkeypatch, {"qgis.core": {"Qgis": _Qgis, "QgsMessageLog": sink}}
        )
        from gratisgis_qgis.log_qgis_handler import QGISLogPanelHandler

        handler = QGISLogPanelHandler()
        handler.emit(_record(logging.ERROR))
        handler.emit(_record(logging.INFO))
        assert [m[2] for m in sink.messages] == ["scoped-critical", "scoped-info"]
        assert sink.messages[0][1] == "GratisGIS"

    def test_legacy_class_attributes_still_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Qgis:
            Info = "legacy-info"
            Warning = "legacy-warning"
            Critical = "legacy-critical"

        sink = _RecorderMessageLog()
        install_qgis_stub(
            monkeypatch, {"qgis.core": {"Qgis": _Qgis, "QgsMessageLog": sink}}
        )
        from gratisgis_qgis.log_qgis_handler import QGISLogPanelHandler

        QGISLogPanelHandler().emit(_record(logging.WARNING))
        assert [m[2] for m in sink.messages] == ["legacy-warning"]

    def test_unresolvable_levels_raise_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Qgis:
            """No message levels anywhere."""

        install_qgis_stub(
            monkeypatch,
            {"qgis.core": {"Qgis": _Qgis, "QgsMessageLog": _RecorderMessageLog()}},
        )
        from gratisgis_qgis.log_qgis_handler import QGISLogPanelHandler

        with pytest.raises(AttributeError):
            QGISLogPanelHandler()


class TestLoggerInitDegrades:
    def test_broken_qgis_enums_do_not_break_get_logger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The plugin-load safety property: with qgis importable but
        # its level enums missing, logger init must swallow the
        # handler failure and keep the file handler.
        class _Qgis:
            """No message levels anywhere."""

        install_qgis_stub(
            monkeypatch,
            {"qgis.core": {"Qgis": _Qgis, "QgsMessageLog": _RecorderMessageLog()}},
        )
        import gratisgis_qgis.log as log

        log.teardown_logging()
        try:
            logger = log.get_logger("gratisgis_qgis.handler_degradation_test")
            root = logging.getLogger("gratisgis_qgis")
            # File handler only; the panel handler failed to build.
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0], logging.handlers.RotatingFileHandler)
            logger.debug("still logs to file")
        finally:
            # Leave the suite in the normal initialized state (next
            # get_logger call rebuilds without the stub in place).
            log.teardown_logging()
            log.get_logger("gratisgis_qgis.handler_degradation_reset")
