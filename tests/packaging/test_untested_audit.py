# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ratchet that is supposed to catch the next ConnectionItem.

A gate nobody has watched fail is a gate nobody knows works, and this
one exists precisely because a silent gap cost a release. So both
directions are exercised: a new untested class fails the build, and a
baseline entry that has since been covered fails it too.

The second is the one that decays quietly if left alone. Without it the
baseline slowly stops describing the debt and turns into a list of
things everyone has agreed not to look at, which is worse than having
no list because it looks like one.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "untested_audit.py"
BASELINE = REPO / "scripts" / "untested_baseline.txt"


def _run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


class TestTheGate:
    def test_the_repo_passes_as_it_stands(self) -> None:
        """Green on the current tree, or the gate is just noise."""
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_new_untested_class_fails_the_build(self) -> None:
        """The case that would have caught the sign-out bug.

        A public class lands in the source tree with nothing naming it.
        That is exactly what ConnectionItem was, at 68% line coverage
        and never once constructed.

        The probe's name is assembled at runtime rather than written
        out, and that detour is not fussiness. The audit's haystack is
        every file under tests/, so a literal name here would appear in
        it and the probe would be reported as covered BY THE TEST THAT
        PLANTED IT. Written the obvious way this test passes against a
        gate that does nothing, which is how it first came out.

        It also shows the gate's real limit honestly: naming a class
        anywhere is enough to satisfy it. Passing means little, failing
        means something definite.
        """
        name = "Probe" + "Surface" + "NobodyMentions"
        victim = REPO / "src" / "gratisgis_qgis" / "_audit_probe.py"
        victim.write_text(f"class {name}:\n    pass\n", encoding="utf-8")
        try:
            result = _run()
        finally:
            victim.unlink()
        assert result.returncode == 1
        assert name in result.stdout
        assert "nothing exercising them" in result.stdout

    def test_a_baseline_entry_that_got_covered_fails_too(self) -> None:
        """The ratchet has to tighten, not just hold.

        Stale entries are how a baseline stops being a debt list. A
        line naming a class that is now exercised must be removed, and
        the build says so rather than letting it sit.
        """
        original = BASELINE.read_text(encoding="utf-8")
        try:
            BASELINE.write_text(
                original + "src/gratisgis_qgis/tasks.py::TaskCancelledError\n",
                encoding="utf-8",
            )
            result = _run()
        finally:
            BASELINE.write_text(original, encoding="utf-8")
        assert result.returncode == 1
        assert "must leave the baseline" in result.stdout
        assert "TaskCancelledError" in result.stdout

    def test_the_baseline_is_empty_and_must_stay_that_way(self) -> None:
        """Every public class is exercised by something. Keep it so.

        The baseline started at twelve entries and is now zero, which
        turns the ratchet into a plain rule: a public class with
        nothing constructing it fails the build, no exceptions
        outstanding. An entry appearing here again is a deliberate act
        and should arrive with a comment saying why.

        Written as an assertion rather than left implicit because an
        empty file is indistinguishable from a clobbered one, and a
        clobbered baseline is the only way this gate goes quiet.
        """
        entries = [
            line.strip()
            for line in BASELINE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert entries == [], (
            "public classes are being excused from the gate again: "
            f"{entries}. If that is deliberate, say why in the file."
        )

    def test_the_baseline_file_still_exists_and_is_readable(self) -> None:
        """Deleting it would silently widen the gate, not narrow it.

        With no file the audit reads an empty baseline, which today is
        the same as the real one. That equivalence is temporary and the
        file is what makes an exemption visible when one is next added.
        """
        assert BASELINE.exists()
        assert "may only shrink" in BASELINE.read_text(encoding="utf-8")

    def test_every_baseline_entry_names_a_class_that_exists(self) -> None:
        """Entries must not outlive the code they excuse.

        A line naming a deleted or renamed class silently grants a
        permanent exemption to nothing at all, and hides the fact that
        the real surface may now be unguarded under its new name.
        """
        import ast

        for line in BASELINE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rel, _, name = line.partition("::")
            path = REPO / rel
            assert path.exists(), f"baseline names a missing file: {rel}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = {
                node.name
                for node in tree.body
                if isinstance(node, ast.ClassDef)
            }
            assert name in defined, f"{rel} no longer defines {name}"
