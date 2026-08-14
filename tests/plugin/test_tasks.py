# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the background-task helper (`gratisgis_qgis.tasks`).

Two layers of coverage:

- The executor seam (``set_task_executor`` + ``run_synchronously``),
  which is what dialog tests would install; its dispatch contract
  must match the QgsTask path.
- The QgsTask scheduling path itself, driven against a stubbed
  ``qgis.core`` whose task manager runs tasks deterministically, so
  the run/finished handoff, cancel precedence, the strong-reference
  set, and the progress signal routing are all pinned without QGIS.
"""
from __future__ import annotations

import enum
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import gratisgis_qgis.tasks as tasks
from gratisgis_qgis.tasks import (
    InlineTaskHandle,
    TaskCancelledError,
    TaskHandle,
    format_error,
    run_in_task,
)
from tests.plugin.conftest import install_qgis_stub


class _Sink:
    """Records which completion callback fired with what."""

    def __init__(self) -> None:
        self.results: list[object] = []
        self.errors: list[BaseException] = []
        self.progress: list[float] = []

    def on_done(self, result: object) -> None:
        self.results.append(result)

    def on_error(self, exc: BaseException) -> None:
        self.errors.append(exc)

    def on_progress(self, pct: float) -> None:
        self.progress.append(pct)


@pytest.fixture
def inline_executor() -> Iterator[None]:
    tasks.set_task_executor(tasks.run_synchronously)
    yield
    tasks.set_task_executor(None)


class TestInlineExecutor:
    def test_result_reaches_on_done(self, inline_executor: None) -> None:
        sink = _Sink()
        run_in_task("t", lambda handle: 41 + 1, sink.on_done, sink.on_error)
        assert sink.results == [42]
        assert sink.errors == []

    def test_exception_reaches_on_error_unwrapped(self, inline_executor: None) -> None:
        sink = _Sink()
        boom = ValueError("boom")

        def fn(_handle: TaskHandle) -> None:
            raise boom

        run_in_task("t", fn, sink.on_done, sink.on_error)
        assert sink.results == []
        assert sink.errors == [boom]

    def test_progress_reaches_on_progress(self, inline_executor: None) -> None:
        sink = _Sink()

        def fn(handle: TaskHandle) -> str:
            handle.set_progress(10.0)
            handle.set_progress(90.0)
            return "ok"

        run_in_task("t", fn, sink.on_done, sink.on_error, on_progress=sink.on_progress)
        assert sink.progress == [10.0, 90.0]
        assert sink.results == ["ok"]

    def test_cancel_collapses_exception_to_cancelled(self, inline_executor: None) -> None:
        # A cancelled worker commonly errors on the way out; the user
        # must see a cancel, not a failure.
        sink = _Sink()

        def fn(handle: TaskHandle) -> None:
            assert isinstance(handle, InlineTaskHandle)
            handle.cancel()
            raise RuntimeError("aborted mid-flight")

        run_in_task("t", fn, sink.on_done, sink.on_error)
        assert sink.results == []
        assert len(sink.errors) == 1
        assert isinstance(sink.errors[0], TaskCancelledError)

    def test_cancelled_fn_returning_result_still_delivers_it(
        self, inline_executor: None
    ) -> None:
        # Batch workers observe the cancel and return a partial
        # outcome; that outcome must arrive via on_done.
        sink = _Sink()

        def fn(handle: TaskHandle) -> str:
            assert isinstance(handle, InlineTaskHandle)
            handle.cancel()
            return "partial"

        run_in_task("t", fn, sink.on_done, sink.on_error)
        assert sink.results == ["partial"]
        assert sink.errors == []

    def test_inline_handle_records_progress_and_cancel(self) -> None:
        handle = InlineTaskHandle()
        handle.set_progress(5.0)
        assert handle.progress == [5.0]
        assert handle.is_canceled() is False
        handle.cancel()
        assert handle.is_canceled() is True


# -----------------------------------------------------------
# QgsTask path against a stubbed qgis.core
# -----------------------------------------------------------


class _StubSignal:
    def __init__(self) -> None:
        self.connections: list[tuple[Callable[..., None], object]] = []

    def connect(
        self, callback: Callable[..., None], connection_type: object = None
    ) -> None:
        self.connections.append((callback, connection_type))

    def emit(self, *args: object) -> None:
        for callback, _ in list(self.connections):
            callback(*args)


class _StubQgsTask:
    # QGIS 3 exposes the flag on the class; the production flag
    # resolver must find it here.
    CanCancel = 2

    def __init__(self, description: str = "", flags: object = None) -> None:
        self._description = description
        self.flags_received = flags
        self._canceled = False
        self.progress_values: list[float] = []
        self.progressChanged = _StubSignal()  # QGIS API name

    def description(self) -> str:
        return self._description

    def setProgress(self, value: float) -> None:  # QGIS API name
        self.progress_values.append(value)
        self.progressChanged.emit(value)

    def isCanceled(self) -> bool:  # QGIS API name
        return self._canceled

    def cancel(self) -> None:
        self._canceled = True


class _StubTaskManager:
    """Runs added tasks like QGIS does (run then finished), either
    immediately or deferred behind ``flush()`` so a test can observe
    the in-flight state."""

    def __init__(self) -> None:
        self.auto = True
        self.pending: list[Any] = []

    def addTask(self, task: Any) -> None:  # QGIS API name
        if self.auto:
            self._execute(task)
        else:
            self.pending.append(task)

    def flush(self) -> None:
        while self.pending:
            self._execute(self.pending.pop(0))

    @staticmethod
    def _execute(task: Any) -> None:
        ok = task.run()
        task.finished(ok)


class _StubConnectionType:
    QueuedConnection = object()


class _StubQt:
    ConnectionType = _StubConnectionType


@pytest.fixture
def task_manager(monkeypatch: pytest.MonkeyPatch) -> _StubTaskManager:
    manager = _StubTaskManager()

    class _StubQgsApplication:
        @staticmethod
        def taskManager() -> _StubTaskManager:  # QGIS API name
            return manager

    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsTask": _StubQgsTask,
                "QgsApplication": _StubQgsApplication,
            },
            "qgis.PyQt.QtCore": {"Qt": _StubQt},
        },
    )
    # The cached task class binds the stub QgsTask; reset around the
    # test so neither a previous run's class nor this one leaks.
    monkeypatch.setattr(tasks, "_fn_task_cls", None)
    return manager


def _schedule(
    sink: _Sink,
    fn: Callable[[TaskHandle], Any],
    *,
    cancelable: bool = True,
    with_progress: bool = False,
) -> Any:
    """run_in_task with the sink's callbacks; returns the task object.

    Typed ``Any`` on purpose: the controller protocol is deliberately
    narrow, while these tests inspect stub internals (flags, signal
    connections) that only exist on the concrete stub task.
    """
    return run_in_task(
        "t",
        fn,
        sink.on_done,
        sink.on_error,
        cancelable=cancelable,
        on_progress=sink.on_progress if with_progress else None,
    )


class TestQgsTaskPath:
    def test_result_delivered_and_reference_dropped(
        self, task_manager: _StubTaskManager
    ) -> None:
        sink = _Sink()
        _schedule(sink, lambda handle: "value")
        assert sink.results == ["value"]
        assert sink.errors == []
        assert not tasks._active_tasks

    def test_task_is_strongly_referenced_while_pending(
        self, task_manager: _StubTaskManager
    ) -> None:
        task_manager.auto = False
        sink = _Sink()
        controller = _schedule(sink, lambda handle: 1)
        assert controller in tasks._active_tasks
        task_manager.flush()
        assert not tasks._active_tasks
        assert sink.results == [1]

    def test_exception_carried_to_on_error(self, task_manager: _StubTaskManager) -> None:
        sink = _Sink()
        boom = ValueError("nope")

        def fn(_handle: TaskHandle) -> None:
            raise boom

        _schedule(sink, fn)
        assert sink.errors == [boom]
        assert not tasks._active_tasks

    def test_cancel_before_run_reports_cancelled(
        self, task_manager: _StubTaskManager
    ) -> None:
        task_manager.auto = False
        sink = _Sink()

        def fn(handle: TaskHandle) -> str:
            if handle.is_canceled():
                raise RuntimeError("cancel artifact")
            return "unreachable"

        controller = _schedule(sink, fn)
        controller.cancel()
        task_manager.flush()
        assert sink.results == []
        assert len(sink.errors) == 1
        assert isinstance(sink.errors[0], TaskCancelledError)

    def test_cancelled_fn_returning_result_still_delivers_it(
        self, task_manager: _StubTaskManager
    ) -> None:
        task_manager.auto = False
        sink = _Sink()

        def fn(handle: TaskHandle) -> str:
            assert handle.is_canceled()
            return "partial"

        controller = _schedule(sink, fn)
        controller.cancel()
        task_manager.flush()
        assert sink.results == ["partial"]
        assert sink.errors == []

    def test_progress_routes_through_queued_signal_connection(
        self, task_manager: _StubTaskManager
    ) -> None:
        sink = _Sink()

        def fn(handle: TaskHandle) -> None:
            handle.set_progress(25.0)
            handle.set_progress(75.0)

        controller = _schedule(sink, fn, with_progress=True)
        assert sink.progress == [25.0, 75.0]
        # The connection must be explicitly queued: emitted from the
        # worker thread, a default connection to a plain callable
        # would run GUI code on that worker.
        _, connection_type = controller.progressChanged.connections[0]
        assert connection_type is _StubConnectionType.QueuedConnection

    def test_progress_is_clamped_to_percent_range(
        self, task_manager: _StubTaskManager
    ) -> None:
        sink = _Sink()

        def fn(handle: TaskHandle) -> None:
            handle.set_progress(-5.0)
            handle.set_progress(140.0)

        controller = _schedule(sink, fn)
        assert controller.progress_values == [0.0, 100.0]

    def test_cancelable_flag_controls_ctor_flags(
        self, task_manager: _StubTaskManager
    ) -> None:
        sink = _Sink()
        with_cancel = _schedule(sink, lambda h: None)
        without_cancel = _schedule(sink, lambda h: None, cancelable=False)
        assert with_cancel.flags_received == _StubQgsTask.CanCancel
        assert without_cancel.flags_received == 0

    def test_crashing_callback_is_contained(self, task_manager: _StubTaskManager) -> None:
        # A broken completion callback must not escape into the task
        # machinery (in QGIS it would poison the task manager).
        def bad_done(_result: object) -> None:
            raise RuntimeError("callback bug")

        run_in_task("t", lambda h: 1, bad_done, lambda exc: None)
        assert not tasks._active_tasks


class TestCancelFlags:
    """Regression cover for the crash that reached a user.

    QGIS 4 / PyQt6 type-checks the QgsTask flags argument and rejects a
    plain ``int`` with "argument 2 has unexpected type 'int'". The
    original code returned a bare ``0`` whenever the flag value was an
    ``int`` instance, on the theory that only PyQt5 exposed ints. Qt6
    flag enums subclass ``int`` too, so that branch fired on exactly the
    binding it was supposed to protect, and every sign-in crashed.

    The stub suite could not catch it (a fabricating stub accepts any
    type), so these tests model both real binding shapes directly. The
    complementary check against a real QGIS lives in
    ``scripts/qgis_smoke.py``.
    """

    class _Pyqt6Task:
        """PyQt6 shape: scoped Flag enum whose members subclass int."""

        class Flag(enum.IntFlag):
            CanCancel = 2
            Hidden = 4

        Flags = Flag

    class _Pyqt5Task:
        """PyQt5 shape: class-level int constants, no scoped holder."""

        CanCancel = 2

    def test_pyqt6_non_cancelable_is_not_a_bare_int(self) -> None:
        flags = tasks._cancel_flags(self._Pyqt6Task, False)
        # The exact assertion the shipped bug violated. `type(...) is
        # not int` rather than `not isinstance(...)`: an IntFlag member
        # IS an int instance, and that is precisely the value PyQt6
        # accepts, so isinstance would reject the correct answer.
        assert type(flags) is not int
        assert isinstance(flags, self._Pyqt6Task.Flag)
        assert not flags  # no flags set

    def test_pyqt6_cancelable_carries_the_flag(self) -> None:
        flags = tasks._cancel_flags(self._Pyqt6Task, True)
        assert type(flags) is not int
        assert flags & self._Pyqt6Task.Flag.CanCancel

    def test_pyqt5_shape_still_resolves(self) -> None:
        assert tasks._cancel_flags(self._Pyqt5Task, True) == 2
        # No scoped Flags holder and int constants: falling back to a
        # plain zero is correct here, because that binding wants one.
        assert tasks._cancel_flags(self._Pyqt5Task, False) == 0


class TestFormatError:
    def test_cancelled(self) -> None:
        assert format_error(TaskCancelledError("x")) == "Cancelled."

    def test_auth_error_gets_actionable_message(self) -> None:
        from gratisgis_client import AuthError

        text = format_error(AuthError("Portal GET /items failed: HTTP 401", status=401))
        assert "Sign in again" in text
        assert "GratisGIS > Connections" in text

    def test_portal_error_includes_status_and_code(self) -> None:
        from gratisgis_client import PortalError

        text = format_error(PortalError("Portal exploded", status=502, code="upstream"))
        assert "Portal exploded" in text
        assert "HTTP 502" in text
        assert "code upstream" in text

    def test_portal_error_does_not_duplicate_status_already_in_message(self) -> None:
        from gratisgis_client import PortalError

        text = format_error(
            PortalError("Portal GET /items failed: HTTP 500", status=500)
        )
        assert text.count("HTTP 500") == 1

    def test_generic_exception_falls_back_to_str(self) -> None:
        assert format_error(RuntimeError("plain")) == "plain"
        assert format_error(RuntimeError()) == "RuntimeError"
