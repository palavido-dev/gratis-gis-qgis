# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background execution for the plugin's long operations.

``run_in_task`` is the one way plugin code moves work off the GUI
thread. It wraps a plain callable in a ``QgsTask`` so QGIS schedules
it on its worker pool, shows it in the task manager UI, and calls us
back on the GUI thread when it ends. The callable receives a
``TaskHandle`` for progress + cancellation instead of touching the
task object directly, which keeps worker functions pure enough to
run under a test executor with no QGIS at all.

Design constraints this module owns:

- ``on_done`` / ``on_error`` must fire on the GUI thread, because
  every caller updates widgets from them. ``QgsTask.finished`` is
  documented to run on the main thread, so the result / exception is
  carried on the task object and dispatched from there.
- Tasks must be strongly referenced while running. QGIS's task
  manager holds C++ ownership but the Python wrapper (and the
  closures it carries) would be garbage-collected mid-run without a
  Python-side reference, which historically manifests as callbacks
  that silently never fire.
- ``qgis`` is imported lazily, inside the scheduling path, so the
  pure-Python test suite can import this module and drive the
  executor seam (or a stubbed ``qgis.core``) without QGIS bindings.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol

from gratisgis_client import AuthError, PortalError

from .log import get_logger

_log = get_logger(__name__)


class TaskCancelledError(Exception):
    """The task ended because the user cancelled it.

    Delivered through ``on_error`` so ``run_in_task`` keeps its
    two-callback shape; dialogs check for this type to reset their UI
    quietly instead of raising an error box at a user who just
    clicked Cancel.
    """


class TaskHandle(Protocol):
    """What a task function receives to talk back to its task."""

    def set_progress(self, pct: float) -> None:
        """Report progress in percent (0..100)."""
        ...

    def is_canceled(self) -> bool:
        """True once cancellation was requested; the function should
        stop at the next safe point."""
        ...


class TaskController(Protocol):
    """What ``run_in_task`` returns to the scheduling side."""

    def cancel(self) -> None:
        """Request cancellation. Safe to call after completion."""
        ...


# Strong references to in-flight tasks; discarded from finished().
_active_tasks: set[object] = set()

# Test seam: when set, run_in_task delegates scheduling to this
# callable instead of building a QgsTask, so pure-Python tests can
# run the function synchronously (see run_synchronously).
_executor: (
    Callable[
        [
            str,
            Callable[[TaskHandle], Any],
            Callable[[Any], None],
            Callable[[BaseException], None],
            bool,
            Callable[[float], None] | None,
        ],
        TaskController,
    ]
    | None
) = None

# Built lazily because it subclasses QgsTask; cached so repeat calls
# do not redefine the class.
_fn_task_cls: type | None = None


def set_task_executor(executor: Any | None) -> None:
    """Install (or clear, with ``None``) the scheduling seam.

    Tests install ``run_synchronously`` here so dialog logic and the
    dispatch contract run inline; production never calls this.
    """
    global _executor
    _executor = executor


def run_in_task(
    description: str,
    fn: Callable[[TaskHandle], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[BaseException], None],
    *,
    cancelable: bool = True,
    on_progress: Callable[[float], None] | None = None,
) -> TaskController:
    """Run ``fn`` off the GUI thread; deliver the outcome back on it.

    Exactly one of ``on_done(result)`` / ``on_error(exc)`` fires, on
    the GUI thread. Cancellation (either via the returned controller
    or the QGIS task manager UI) is reported as ``on_error`` with a
    ``TaskCancelledError`` whenever the function raised or bailed,
    because a cancelled worker commonly errors on the way out and the
    user must not see that as a failure. A function that observes the
    cancel and still RETURNS a value gets that value delivered via
    ``on_done``; that is how a batch worker reports how far it got.

    ``on_progress`` receives the percent values the function reports
    through its handle, also on the GUI thread, so dialogs can mirror
    the QGIS task-manager progress bar in their own UI.
    """
    if _executor is not None:
        return _executor(description, fn, on_done, on_error, cancelable, on_progress)
    return _schedule_qgs_task(description, fn, on_done, on_error, cancelable, on_progress)


class InlineTaskHandle:
    """Handle + controller used by the synchronous executor.

    Public so tests can construct one directly when exercising a task
    function without going through ``run_in_task`` at all.
    """

    def __init__(self, on_progress: Callable[[float], None] | None = None) -> None:
        self.progress: list[float] = []
        self._canceled = threading.Event()
        self._forward = on_progress

    def set_progress(self, pct: float) -> None:
        self.progress.append(pct)
        if self._forward is not None:
            self._forward(pct)

    def is_canceled(self) -> bool:
        return self._canceled.is_set()

    def cancel(self) -> None:
        self._canceled.set()


def run_synchronously(
    description: str,
    fn: Callable[[TaskHandle], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[BaseException], None],
    cancelable: bool,
    on_progress: Callable[[float], None] | None,
) -> TaskController:
    """Executor that runs ``fn`` inline on the calling thread.

    Mirrors the dispatch contract of the QgsTask path (single
    callback, cancel wins) so a test that swaps this in observes the
    same behavior the plugin has inside QGIS, minus the threading.
    """
    handle = InlineTaskHandle(on_progress)
    try:
        result = fn(handle)
    except BaseException as exc:
        if handle.is_canceled() and not isinstance(exc, TaskCancelledError):
            on_error(TaskCancelledError(f"{description} was cancelled"))
        else:
            on_error(exc)
        return handle
    on_done(result)
    return handle


def format_error(exc: BaseException) -> str:
    """One consistent user-facing line for a task failure.

    Auth failures get the actionable sentence instead of the raw
    HTTP noise because the only fix is signing in again; portal
    errors keep their message but always surface status + error code
    so bug reports carry enough to diagnose.
    """
    if isinstance(exc, TaskCancelledError):
        return "Cancelled."
    if isinstance(exc, AuthError):
        return (
            "Your session has expired or you lack access. "
            "Sign in again from GratisGIS > Connections."
        )
    if isinstance(exc, PortalError):
        message = str(exc) or "Portal request failed"
        details: list[str] = []
        if exc.status is not None and f"HTTP {exc.status}" not in message:
            details.append(f"HTTP {exc.status}")
        if exc.code:
            details.append(f"code {exc.code}")
        if details:
            return f"{message} ({', '.join(details)})"
        return message
    return str(exc) or type(exc).__name__


# -----------------------------------------------------------
# QgsTask path
# -----------------------------------------------------------


class _QgsTaskHandle:
    """Adapter giving the task function the narrow handle surface."""

    def __init__(self, task: Any) -> None:
        self._task = task

    def set_progress(self, pct: float) -> None:
        # QgsTask clamps to 0..100 itself, but clamping here keeps
        # the reported values identical between this path and the
        # inline executor.
        self._task.setProgress(max(0.0, min(100.0, float(pct))))

    def is_canceled(self) -> bool:
        return bool(self._task.isCanceled())


def _cancel_flags(qgs_task_cls: Any, cancelable: bool) -> Any:
    """Flags value for the QgsTask constructor across QGIS 3 and 4.

    Never return a bare ``int``. PyQt6 type-checks this argument
    strictly and rejects ``0`` with "argument 2 has unexpected type
    'int'", which is a plugin-load-visible crash on the first task.
    An earlier version guarded that with ``isinstance(can_cancel, int)``
    on the theory that only PyQt5 exposed ints, but Qt6 flag enums
    subclass ``int`` too, so the guard fired on exactly the build it was
    meant to protect. Probed against QGIS 4.0.2 / PyQt6: ``Flags()``,
    ``Flags(0)`` and ``type(CanCancel)(0)`` are all accepted, a raw
    ``CanCancel`` is accepted, and both ``0`` and ``CanCancel &
    ~CanCancel`` (which also collapses to a plain int) are rejected.
    """
    holder = getattr(qgs_task_cls, "Flag", qgs_task_cls)
    can_cancel = holder.CanCancel
    if cancelable:
        return can_cancel
    # Prefer the QFlags wrapper: it is the declared parameter type on
    # both bindings. Fall back to the flag enum's own zero, then to a
    # plain int for any binding old enough to want one.
    flags_cls = getattr(qgs_task_cls, "Flags", None)
    for build in (
        (lambda: flags_cls()) if flags_cls is not None else None,
        lambda: type(can_cancel)(0),
    ):
        if build is None:
            continue
        try:
            return build()
        except Exception:  # pragma: no cover - binding-specific
            _log.debug("task flag constructor failed", exc_info=True)
            continue
    return 0  # pragma: no cover - no known binding reaches this


def _build_fn_task_cls() -> type:
    from qgis.core import QgsTask  # type: ignore[import-not-found]

    class _FnTask(QgsTask):  # type: ignore[misc]
        """QgsTask wrapper around one plain callable.

        The result / exception ride on the instance between ``run``
        (worker thread) and ``finished`` (main thread); QGIS
        guarantees ``finished`` runs after ``run`` returns, so no
        further synchronization is needed.
        """

        def __init__(
            self,
            description: str,
            fn: Callable[[TaskHandle], Any],
            on_done: Callable[[Any], None],
            on_error: Callable[[BaseException], None],
            cancelable: bool,
        ) -> None:
            super().__init__(description, _cancel_flags(QgsTask, cancelable))
            self._fn = fn
            self._on_done = on_done
            self._on_error = on_error
            self._result: Any = None
            self._exc: BaseException | None = None

        def run(self) -> bool:  # QGIS API name
            handle = _QgsTaskHandle(self)
            try:
                self._result = self._fn(handle)
            except BaseException as exc:
                # Carry the exception to finished() instead of letting
                # it escape: QGIS logs escaped exceptions but never
                # reports them to the caller, which reads as a publish
                # that silently never completes.
                self._exc = exc
                return False
            return True

        def finished(self, result: bool) -> None:  # QGIS API name
            _active_tasks.discard(self)
            try:
                if result:
                    # The function returned a value; deliver it even
                    # after a cancel request, because a worker that
                    # observed the cancel and still returned is
                    # reporting a coherent partial outcome.
                    self._on_done(self._result)
                    return
                if isinstance(self._exc, TaskCancelledError):
                    self._on_error(self._exc)
                    return
                if self.isCanceled():
                    # Any exception raised on the way out of a
                    # cancelled worker is a cancellation artifact,
                    # not a failure the user should be shown.
                    self._on_error(
                        TaskCancelledError(f"{self.description()} was cancelled")
                    )
                    return
                if self._exc is not None:
                    self._on_error(self._exc)
                    return
                # run() returned False without an exception and
                # without a cancel: nothing in this module produces
                # that, so treat it as the bug it would be.
                self._on_error(RuntimeError(f"{self.description()} failed without an error"))
            except Exception:
                # A crash inside a completion callback must not
                # propagate into QGIS's task machinery, where it
                # would poison the task manager for every plugin.
                _log.exception("Task completion callback failed: %s", self.description())

    return _FnTask


def _schedule_qgs_task(
    description: str,
    fn: Callable[[TaskHandle], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[BaseException], None],
    cancelable: bool,
    on_progress: Callable[[float], None] | None,
) -> TaskController:
    global _fn_task_cls
    if _fn_task_cls is None:
        _fn_task_cls = _build_fn_task_cls()
    task = _fn_task_cls(description, fn, on_done, on_error, cancelable)
    if on_progress is not None:
        from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]

        # Explicitly queued: progressChanged is emitted from the
        # worker thread, and PyQt runs plain-callable connections in
        # the emitter's thread unless told otherwise. Queued delivery
        # proxies through the connecting (GUI) thread's event loop,
        # which is where widget updates belong.
        task.progressChanged.connect(on_progress, Qt.ConnectionType.QueuedConnection)
    _active_tasks.add(task)
    from qgis.core import QgsApplication  # type: ignore[import-not-found]

    QgsApplication.taskManager().addTask(task)
    return task
