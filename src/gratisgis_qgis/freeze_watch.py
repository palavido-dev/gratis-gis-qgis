# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catch a frozen GUI thread and write down what it was doing.

QGIS can hang hard while opening a project that contains portal
layers, and the plugin's own log is no help: the last lines are
"plugin instantiated" and "initGui", with no error and no traceback.
That silence is the actual obstacle. It is not evidence that plugin
code is innocent, it is evidence that nothing was watching.

The hang is believed to start in QGIS's own auth manager rather than
in plugin Python. A layer URI carrying ``authcfg=`` makes QGIS resolve
that config through ``QgsAuthManager``, which needs the auth database
unlocked, which raises a modal master-password prompt. QGIS has a
documented history of deadlocking there: the credential dialog calls
``verifyMasterPassword()``, which reaches ``authDatabaseConnection()``
and blocks on a mutex (QGIS issue 35993), and the same manager hangs
when reached without a loaded master key (issue 51317). A prompt
raised once per layer during project load, possibly off the GUI
thread, is a plausible way into it.

Believed is not known, which is the point of this module. It detects
that the GUI thread has stopped running events and dumps the stacks of
every Python thread while the process is still wedged, so the next
freeze produces evidence instead of a Task Manager kill.

How it works: a QTimer on the GUI thread records a monotonic
timestamp; a daemon thread watches that timestamp go stale. Detection
therefore lives off the GUI thread, which is the only way to observe a
GUI thread that is not running.

Two deliberate limits:

- The dump is Python-level. If the deadlock is entirely inside Qt or
  GDAL C++, the GUI thread's Python stack shows the last Python frame
  that called into C++, which names the operation but not the lock.
  ``docs/diagnosing-a-freeze.md`` covers getting a native stack.
- It reports; it never intervenes. Nothing here tries to break a
  deadlock, time out a request, or kill a thread. A watchdog that
  takes action during an unexplained hang is a second unexplained
  behaviour on top of the first.
"""
from __future__ import annotations

import faulthandler
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .log import get_logger

_log = get_logger(__name__)

#: Seconds of unresponsiveness before the GUI thread counts as stalled.
#:
#: Well above any legitimate blocking call: rendering a heavy layer or
#: a slow synchronous HTTP request can hold the GUI thread for several
#: seconds and is not what this is looking for. A freeze that needs
#: Task Manager lasts until the user kills it, so nothing is lost by
#: waiting. Too low and the log fills with dumps of ordinary slowness,
#: which is how a diagnostic gets switched off and stops being there
#: on the day it matters.
STALL_SECONDS = 10.0

#: How often the GUI timer ticks, and how often the watcher looks.
TICK_SECONDS = 1.0

#: Environment variable that forces the watchdog off.
DISABLE_ENV = "GRATISGIS_NO_FREEZE_WATCHDOG"


class StallDetector:
    """Decides when a stall has begun and when it has ended.

    Pure: no QGIS, no Qt, no threads, no clock of its own. Every time
    value is passed in. That is what lets the interesting behaviour
    (dump once per stall, not once per poll; report recovery) be tested
    for real rather than through a fake that agrees with itself.
    """

    def __init__(self, stall_seconds: float = STALL_SECONDS) -> None:
        self._stall_seconds = stall_seconds
        self._last_tick: float | None = None
        self._stalled = False

    def tick(self, now: float) -> None:
        """Record that the GUI thread ran an event at ``now``."""
        self._last_tick = now
        self._stalled = False

    @property
    def stalled(self) -> bool:
        return self._stalled

    def poll(self, now: float) -> float | None:
        """Check for a stall that has not been reported yet.

        Returns the stall duration in seconds the first time a stall is
        seen, then None for the rest of that same stall however long it
        lasts. One dump per freeze: repeating it every second would bury
        the first and most useful one, which is the one taken closest to
        the moment the lock was taken.

        Returns None before the first tick, so a watcher started ahead
        of the GUI timer cannot report the gap between them as a stall.
        """
        if self._last_tick is None or self._stalled:
            return None
        elapsed = now - self._last_tick
        if elapsed < self._stall_seconds:
            return None
        self._stalled = True
        return elapsed

    def recovered(self, now: float) -> float | None:
        """Stall duration if the GUI thread just came back, else None.

        A freeze the user waited out rather than killed is worth a line
        in the log too: it dates the stall and bounds how long it held,
        which a dump alone does not say.
        """
        if not self._stalled or self._last_tick is None:
            return None
        return now - self._last_tick


def dump_path(log_dir: Path, when: float) -> Path:
    """Where one freeze dump goes.

    A file per freeze, named by local time, so several stalls in one
    session stay separate and a user can say which one they hit.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    return log_dir / f"freeze-{stamp}.txt"


def _hex_width() -> int:
    """Digits ``faulthandler`` pads a thread id to.

    It renders ids through CPython's ``_Py_DumpHexadecimal`` at the
    width of a C ``unsigned long``: 8 hex digits on Windows, 16 on
    64-bit Linux and macOS. The roster has to match exactly, or the
    reader cannot find "0x00008d4c" from the table in the stacks below
    it, and a table that does not join is worse than none because it
    looks like it should work.

    Derived rather than hardcoded because this is developed on Windows
    and CI runs Linux, so a hardcoded width would be right in exactly
    one of the two places and nobody would notice which.
    """
    try:
        import ctypes

        return ctypes.sizeof(ctypes.c_ulong) * 2
    except Exception:
        return 16


def thread_roster() -> str:
    """A hex-id to name table for the threads alive right now.

    ``faulthandler`` identifies threads only by hex id: every stack in
    the dump is headed "Thread 0x00008d4c" with no name attached. In a
    QGIS process that is a dozen indistinguishable numbers, and picking
    the GUI thread out of them is guesswork. Python knows the names, so
    the dump carries the translation.

    The main thread is called out because it is the one that matters:
    in QGIS the main thread IS the GUI thread, and what that thread was
    doing is the entire question the dump exists to answer.
    """
    width = _hex_width()
    lines = []
    try:
        main = threading.main_thread()
        for thread in threading.enumerate():
            ident = thread.ident
            if ident is None:
                continue
            note = "  <- GUI thread" if thread is main else ""
            lines.append(f"  0x{ident:0{width}x}  {thread.name}{note}")
    except Exception:
        return "  (thread roster unavailable)\n"
    if not lines:
        return "  (no threads reported)\n"
    return "\n".join(sorted(lines)) + "\n"


def write_dump(path: Path, elapsed: float) -> bool:
    """Dump every Python thread's stack to ``path``. False on failure.

    Uses a plain file object and ``faulthandler``, not the logging
    stack, on purpose. If the GUI thread froze while inside a logging
    handler it still holds that handler's lock, and a watchdog that
    tried to log first would block on it and take the evidence down
    with it. The dump lands first, by the route with the fewest locks
    between here and the disk; logging about it comes after.

    ``dump_traceback`` is deliberately not ``faulthandler.enable()``:
    this writes one dump on request and leaves the fatal-error handler
    alone, because installing that would change how QGIS behaves on a
    segfault, which is not this module's business.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "GratisGIS freeze dump\n"
                f"written: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"GUI thread unresponsive for: {elapsed:.1f}s\n"
                f"pid: {os.getpid()}\n"
                "\n"
                "Threads by id (faulthandler below names them in hex only):\n"
            )
            handle.write(thread_roster())
            handle.write(
                "\n"
                "Python stacks for every thread follow. Read the GUI thread's\n"
                "first: its last frame is the call that did not return.\n"
                '"Current thread" is this watchdog, not the frozen one.\n'
                "A deadlock entirely inside Qt or GDAL C++ shows only the\n"
                "Python frame that called into it; see\n"
                "docs/diagnosing-a-freeze.md for a native stack.\n"
                "\n"
            )
            handle.flush()
            faulthandler.dump_traceback(file=handle, all_threads=True)
            handle.flush()
        return True
    except Exception:
        return False


class FreezeWatchdog:
    """Wires a GUI-thread heartbeat to an off-thread watcher.

    Held by the plugin for the session and stopped on unload. The
    watcher is a daemon thread so a stop that never arrives (a QGIS
    exit while the GUI thread is wedged, which is exactly the scenario)
    cannot keep the process alive.
    """

    def __init__(
        self,
        log_dir: Path,
        *,
        stall_seconds: float = STALL_SECONDS,
        tick_seconds: float = TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log_dir = log_dir
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._detector = StallDetector(stall_seconds)
        self._timer: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        """Begin watching. False when disabled or unavailable.

        Never raises. A diagnostic that can break plugin load is worse
        than no diagnostic, and this runs inside ``initGui``.
        """
        if os.environ.get(DISABLE_ENV):
            _log.debug("freeze watchdog disabled by %s", DISABLE_ENV)
            return False
        if self._thread is not None:
            return True
        try:
            from qgis.PyQt.QtCore import QTimer  # type: ignore[import-not-found]

            self._detector.tick(self._clock())
            timer = QTimer()
            timer.setInterval(int(self._tick_seconds * 1000))
            timer.timeout.connect(self._on_tick)
            timer.start()
            self._timer = timer
        except Exception:
            _log.debug("freeze watchdog could not start", exc_info=True)
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch, name="gratisgis-freeze-watchdog", daemon=True
        )
        self._thread.start()
        _log.debug(
            "freeze watchdog started; a GUI stall over %.0fs will be dumped to %s",
            self._detector._stall_seconds,
            self._log_dir,
        )
        return True

    def stop(self) -> None:
        """Stop watching. Safe to call when never started."""
        self._stop.set()
        if self._timer is not None:
            try:
                self._timer.stop()
                self._timer.deleteLater()
            except Exception:
                _log.debug("freeze watchdog timer teardown failed", exc_info=True)
            self._timer = None
        self._thread = None

    def _on_tick(self) -> None:
        """GUI thread heartbeat. Must stay trivial."""
        now = self._clock()
        recovered = self._detector.recovered(now)
        self._detector.tick(now)
        if recovered is not None:
            _log.warning(
                "The QGIS window was unresponsive for about %.0fs and has "
                "recovered. A stack dump was written to %s",
                recovered,
                self._log_dir,
            )

    def _watch(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                elapsed = self._detector.poll(self._clock())
            except Exception:
                continue
            if elapsed is None:
                continue
            path = dump_path(self._log_dir, time.time())
            ok = write_dump(path, elapsed)
            # Logging comes after the dump is safely on disk; see
            # write_dump. It can still block if the frozen GUI thread
            # holds the handler lock, and losing the log line is an
            # acceptable trade for never losing the dump.
            if ok:
                _log.error(
                    "The QGIS window has been unresponsive for %.0fs. Wrote a "
                    "stack dump to %s. Please attach that file to the issue.",
                    elapsed,
                    path,
                )
            else:
                _log.error(
                    "The QGIS window has been unresponsive for %.0fs and the "
                    "stack dump could not be written to %s",
                    elapsed,
                    path,
                )
