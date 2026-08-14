# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detecting a frozen GUI thread, and writing down what it was doing.

The freeze under investigation kills QGIS hard enough to need Task
Manager, and left a plugin log whose last line was "initGui". These
cover the part that decides a stall has happened, plus the dump file
itself.

The decision logic is tested against a clock that is passed in rather
than a real one, so the awkward cases (dump once per freeze not once
per poll; do not call the gap before the first heartbeat a stall) are
asserted directly instead of waited for. The Qt timer and the watcher
thread around it are not covered here; whether QTimer fires during a
project load is a question about real QGIS and belongs in
scripts/qgis_smoke.py, where the answer means something.
"""
from __future__ import annotations

import faulthandler
import re
import threading
from pathlib import Path

from gratisgis_qgis.freeze_watch import (
    StallDetector,
    dump_path,
    write_dump,
)


def _roster_section(text: str) -> str:
    """The id-to-name table, without the stacks that follow it.

    Sliced out so the roster assertions cannot be satisfied by the
    faulthandler output further down the same file, which contains
    every id too and would make a missing roster look present.
    """
    start = text.index("Threads by id")
    end = text.index("Python stacks for every thread")
    return text[start:end]


class TestStallDetector:
    """When a stall starts, and when it is over."""

    def test_no_stall_before_the_first_heartbeat(self) -> None:
        """A watcher that starts before the GUI timer must stay quiet.

        Both are started from initGui, in that order, so this gap is
        real. Reporting it would mean every QGIS launch wrote a freeze
        dump, and a diagnostic that cries wolf at startup is one the
        user turns off before the day it matters.
        """
        detector = StallDetector(stall_seconds=10.0)
        assert detector.poll(1_000_000.0) is None

    def test_responsive_gui_never_stalls(self) -> None:
        detector = StallDetector(stall_seconds=10.0)
        now = 100.0
        for _ in range(20):
            detector.tick(now)
            now += 1.0
            assert detector.poll(now) is None
        assert not detector.stalled

    def test_stall_reported_once_it_passes_the_threshold(self) -> None:
        detector = StallDetector(stall_seconds=10.0)
        detector.tick(100.0)
        assert detector.poll(105.0) is None, "5s is not yet a stall"
        elapsed = detector.poll(111.0)
        assert elapsed is not None
        assert elapsed == 11.0
        assert detector.stalled

    def test_a_long_freeze_dumps_once_not_every_poll(self) -> None:
        """One dump per freeze.

        A freeze lasts until the user kills QGIS, so a watcher polling
        every second would write hundreds of files and bury the first
        one. The first is the one worth having: it is taken closest to
        the moment the lock was taken.
        """
        detector = StallDetector(stall_seconds=10.0)
        detector.tick(100.0)
        assert detector.poll(111.0) is not None
        for now in (112.0, 120.0, 300.0, 5_000.0):
            assert detector.poll(now) is None

    def test_recovery_is_reported_with_how_long_it_took(self) -> None:
        """A freeze the user waited out is still worth a log line."""
        detector = StallDetector(stall_seconds=10.0)
        detector.tick(100.0)
        assert detector.poll(115.0) is not None
        assert detector.recovered(118.0) == 18.0

    def test_recovery_is_silent_when_there_was_no_stall(self) -> None:
        detector = StallDetector(stall_seconds=10.0)
        detector.tick(100.0)
        assert detector.recovered(101.0) is None

    def test_a_stall_can_be_detected_again_after_recovery(self) -> None:
        """Two freezes in one session are two reports.

        The once-per-freeze guard has to reset, or a user who hit a
        stall early on gets no dump for the one they actually report.
        """
        detector = StallDetector(stall_seconds=10.0)
        detector.tick(100.0)
        assert detector.poll(120.0) is not None
        detector.tick(121.0)  # GUI came back
        assert detector.poll(122.0) is None
        assert detector.poll(140.0) is not None


class TestDumpPath:
    def test_each_freeze_gets_its_own_file(self, tmp_path: Path) -> None:
        first = dump_path(tmp_path, 1_700_000_000.0)
        second = dump_path(tmp_path, 1_700_000_060.0)
        assert first != second
        assert first.parent == tmp_path
        assert first.name.startswith("freeze-")
        assert first.suffix == ".txt"


class TestWriteDump:
    def test_dump_covers_threads_other_than_the_caller(self, tmp_path: Path) -> None:
        """all_threads, not just the caller's.

        The whole point is the stack of a thread that is not running
        this code. A dump of only the watchdog's own stack would say
        nothing about the frozen GUI thread, and would still look like
        a working diagnostic in the log.
        """
        started = threading.Event()
        release = threading.Event()

        def park() -> None:
            started.set()
            release.wait(5.0)

        other = threading.Thread(target=park, name="parked-for-the-test")
        other.start()
        try:
            assert started.wait(5.0)
            path = tmp_path / "freeze.txt"
            assert write_dump(path, 12.5)
            text = path.read_text(encoding="utf-8")
        finally:
            release.set()
            other.join(5.0)

        assert "12.5" in text, "how long it was stuck is the headline fact"
        # faulthandler heads the caller "Current thread 0x..." and every
        # other thread "Thread 0x...". Both spellings must be present or
        # the dump only covered the thread that asked for it.
        assert "Current thread 0x" in text
        assert "\nThread 0x" in text, "the parked thread must be in there"

    def test_dump_translates_thread_ids_to_names(self, tmp_path: Path) -> None:
        """faulthandler names threads in hex; the dump must decode them.

        Without this the dump is a dozen numbered stacks in a QGIS
        process and no way to tell which one is the GUI thread, which
        is the only stack anybody opens the file for. Found by writing
        the test first and watching it fail against a dump that had the
        stacks and none of the names.
        """
        started = threading.Event()
        release = threading.Event()

        def park() -> None:
            started.set()
            release.wait(5.0)

        other = threading.Thread(target=park, name="parked-for-the-test")
        other.start()
        try:
            assert started.wait(5.0)
            path = tmp_path / "freeze.txt"
            assert write_dump(path, 1.0)
            text = path.read_text(encoding="utf-8")
        finally:
            release.set()
            other.join(5.0)

        roster = _roster_section(text)
        assert "parked-for-the-test" in roster, "names must be in the roster"
        assert other.ident is not None
        # Paired with an id, not floating on its own: a list of names
        # with no ids joins to nothing in the stacks below.
        assert re.search(
            r"0x[0-9a-f]+\s+parked-for-the-test", roster
        ), "every name must carry its hex id"
        assert "<- GUI thread" in roster, "the one stack that matters is marked"

    def test_roster_hex_matches_the_faulthandler_spelling(self, tmp_path: Path) -> None:
        """The two halves have to agree or the table is useless.

        faulthandler pads to the width of a C unsigned long: 8 hex
        digits on Windows, 16 on 64-bit Linux. A roster that spelled the
        same id differently (wrong padding, uppercase, decimal) would
        look correct in isolation and still not let anyone match a name
        to a stack. This runs on both platforms, which is the point:
        development is on Windows and CI is Linux, so a hardcoded width
        would pass in one place and fail in the other.
        """
        path = tmp_path / "freeze.txt"
        assert write_dump(path, 1.0)
        text = path.read_text(encoding="utf-8")
        main_ident = threading.main_thread().ident
        assert main_ident is not None
        # Take the spelling from the dump itself rather than restating
        # the format here, so this cannot agree with a wrong roster.
        match = re.search(r"Current thread (0x[0-9a-f]+)", text)
        assert match is not None
        assert match.group(1) in _roster_section(text), (
            "the id faulthandler printed must appear in the roster verbatim"
        )

    def test_dump_survives_an_unwritable_path(self, tmp_path: Path) -> None:
        """Returns False instead of raising.

        This runs on a watchdog thread during a freeze. An exception
        there would kill the watcher, so the user would lose the dump
        AND every later one, on the one occasion they needed it.
        """
        target = tmp_path / "not-a-dir"
        target.write_text("blocking the path", encoding="utf-8")
        assert write_dump(target / "freeze.txt", 3.0) is False

    def test_dump_leaves_faulthandler_as_it_found_it(self, tmp_path: Path) -> None:
        """Must not switch faulthandler on or off behind QGIS's back.

        ``dump_traceback`` is a one-shot call and does not enable the
        fatal-error handler, which would change how QGIS behaves on a
        segfault. Pinned because reaching for ``faulthandler.enable()``
        is the obvious next edit to make here and it would be wrong.
        """
        before = faulthandler.is_enabled()
        write_dump(tmp_path / "freeze.txt", 1.0)
        assert faulthandler.is_enabled() == before
