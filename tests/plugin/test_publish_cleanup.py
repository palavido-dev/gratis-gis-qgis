# SPDX-License-Identifier: AGPL-3.0-or-later
"""Orphaned-item cleanup in the raster publish, plus the poll reset.

The publish flows create a portal item and then perform further
fallible calls with no transaction across them, so a post-create
failure has to delete the created item (best-effort), record what
happened for the user, and re-raise the original error.

The raster half lives here; the vector half moved to
test_vector_pipeline.py along with the code it covers. The Qt widget
classes are stubbed just enough for the dialog modules to import.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from gratisgis_qgis.tasks import InlineTaskHandle, TaskCancelledError
from tests.plugin.conftest import install_qgis_stub

_WIDGET_NAMES = [
    "QApplication",
    "QCheckBox",
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QFileDialog",
    "QFormLayout",
    "QHBoxLayout",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QMessageBox",
    "QPlainTextEdit",
    "QProgressBar",
    "QPushButton",
    "QTextEdit",
    "QVBoxLayout",
    "QWidget",
]


def _widget_stubs() -> dict[str, object]:
    return {name: type(name, (), {}) for name in _WIDGET_NAMES}


@pytest.fixture
def raster_dialog_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QTimer": type("QTimer", (), {}),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": _widget_stubs(),
        },
    )
    import gratisgis_qgis.ui.publish_raster_dialog as mod

    return mod


@pytest.fixture
def vector_dialog_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsCoordinateReferenceSystem": type("QgsCoordinateReferenceSystem", (), {}),
                "QgsCoordinateTransformContext": type("QgsCoordinateTransformContext", (), {}),
                "QgsProject": type("QgsProject", (), {}),
                    "QgsRasterLayer": type("QgsRasterLayer", (), {}),
                "QgsVectorFileWriter": type("QgsVectorFileWriter", (), {}),
                "QgsVectorLayer": type("QgsVectorLayer", (), {}),
                "QgsWkbTypes": type("QgsWkbTypes", (), {}),
            },
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QTimer": type("QTimer", (), {}),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": _widget_stubs(),
        },
    )
    import gratisgis_qgis.ui.publish_vector_dialog as mod

    return mod


class _FakeItems:
    def __init__(self, *, delete_raises: Exception | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._delete_raises = delete_raises

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(id="item-1")

    def delete(self, item_id: str) -> None:
        if self._delete_raises is not None:
            raise self._delete_raises
        self.deleted.append(item_id)


class TestRasterPipelineCleanup:
    def _fake_client(self, *, delete_raises: Exception | None = None) -> SimpleNamespace:
        items = _FakeItems(delete_raises=delete_raises)
        storage = SimpleNamespace(
            check_tile_layer_space=lambda **kw: SimpleNamespace(ok=True, reason=None),
            presign_upload=self._presign_boom,
        )
        return SimpleNamespace(items=items, storage=storage)

    @staticmethod
    def _presign_boom(**_kwargs: Any) -> None:
        raise RuntimeError("presign exploded")

    def _run(
        self,
        mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        client: SimpleNamespace,
        *,
        expect: type[BaseException],
        handle: InlineTaskHandle | None = None,
    ) -> tuple[list[str], BaseException]:
        monkeypatch.setattr(mod, "get_client", lambda _profile: client)
        notes: list[str] = []
        with pytest.raises(expect) as excinfo:
            mod.run_raster_pipeline(
                handle if handle is not None else InlineTaskHandle(),
                profile=SimpleNamespace(name="demo", verify_tls=True),
                file_path="/tmp/dem.tif",
                file_name="dem.tif",
                size=1234,
                title="DEM",
                description=None,
                access="private",
                needs_server_conversion=True,
                cleanup_notes=notes,
            )
        return notes, excinfo.value

    def test_failure_after_create_deletes_item_and_notes_it(
        self, raster_dialog_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._fake_client()
        notes, exc = self._run(
            raster_dialog_mod, monkeypatch, client, expect=RuntimeError
        )
        # The original error propagates unchanged...
        assert "presign exploded" in str(exc)
        # ...and the empty tile_layer item did not get stranded.
        assert client.items.deleted == ["item-1"]
        assert notes == ["The partly created tile layer item was removed from the portal."]

    def test_failed_cleanup_is_reported_not_raised(
        self, raster_dialog_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._fake_client(delete_raises=RuntimeError("delete refused"))
        notes, exc = self._run(
            raster_dialog_mod, monkeypatch, client, expect=RuntimeError
        )
        # The pipeline's own error wins; the cleanup failure only
        # changes what the user is told.
        assert "presign exploded" in str(exc)
        assert len(notes) == 1
        assert "item-1" in notes[0]
        assert "could not be removed" in notes[0]

    def test_cancel_after_create_also_cleans_up(
        self, raster_dialog_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A cancelled publish strands the empty item exactly like a
        # failure; the cancel must still present as a cancel.
        client = self._fake_client()
        handle = InlineTaskHandle()
        handle.cancel()
        notes, _exc = self._run(
            raster_dialog_mod,
            monkeypatch,
            client,
            expect=TaskCancelledError,
            handle=handle,
        )
        assert client.items.deleted == ["item-1"]
        assert notes and "removed" in notes[0]


# The vector half of this file has moved to test_vector_pipeline.py.
# Its cleanup logic left publish_vector_dialog for publish/vector_pipeline.py,
# where it is covered alongside the staging and probe steps it belongs
# with, and without needing a single Qt stub to reach it.


class _FakeProgressBar:
    def __init__(self) -> None:
        self.visible: bool | None = None
        self.range: tuple[int, int] | None = None
        self.value: int | None = None

    def setVisible(self, visible: bool) -> None:  # Qt API name
        self.visible = visible

    def setRange(self, lo: int, hi: int) -> None:  # Qt API name
        self.range = (lo, hi)

    def setValue(self, value: int) -> None:  # Qt API name
        self.value = value


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:  # Qt API name
        self.text = text


class TestPollErrorReset:
    def test_reset_clears_job_state_and_unbusies(
        self, vector_dialog_mod: ModuleType
    ) -> None:
        # A poll failure used to leave _current_job (and friends)
        # populated with the progress surface stuck, so the next
        # Publish click double-created. Driven against a duck-typed
        # self because the reset method touches only these members.
        busy_calls: list[bool] = []
        fake = SimpleNamespace(
            _current_job=object(),
            _current_item_id="item-1",
            _current_layer_id="parcels",
            _current_profile_name="demo",
            _progress_bar=_FakeProgressBar(),
            _progress_label=_FakeLabel(),
            _set_busy=busy_calls.append,
        )
        vector_dialog_mod.PublishLayerDialog._reset_after_poll_error(fake, "boom")

        assert fake._current_job is None
        assert fake._current_item_id is None
        assert fake._current_layer_id is None
        assert fake._current_profile_name is None
        assert fake._progress_bar.visible is False
        # The bar may have been switched to indeterminate (0, 0) by a
        # running import; the reset must restore a determinate range.
        assert fake._progress_bar.range == (0, 100)
        assert fake._progress_bar.value == 0
        assert "boom" in fake._progress_label.text
        assert busy_calls == [False]
