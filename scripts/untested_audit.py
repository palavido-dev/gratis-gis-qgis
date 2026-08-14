# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail when a public class has nothing exercising it, and ratchet.

Line coverage answers "how much of this ran". It does not answer the
question that actually cost us a release: is there anything at all
that builds this class and asks it a question?

``ConnectionItem`` was 68% covered inside ``browser/items.py`` and yet
no test or smoke check had ever constructed one. The uncovered lines
were the ones that mattered, and the percentage said the file was
fine. A user signed out and kept being offered private layers. This
script asks the blunter question instead.

It is a ratchet, not a threshold. The classes untested today are
listed in ``untested_baseline.txt`` so CI is green as it stands, and:

- a NEW untested public class fails the build, which is the case that
  would have caught the tree bug before it shipped;
- a baseline entry that has SINCE been covered also fails, with an
  instruction to delete the line.

The second half is what stops the list going stale. A baseline nobody
prunes stops describing the debt and becomes a list of things everyone
has agreed not to look at.

Run ``python scripts/untested_audit.py`` to check, and
``--update-baseline`` to rewrite the file after deliberately adding or
removing a surface.

Deliberately crude about what counts as "exercised": the class name
appearing anywhere under ``tests/`` or ``scripts/``. A name can be
mentioned without being tested well, so passing here means very
little. Failing here means something definite, and that asymmetry is
the point: this catches a category of blindness, it does not certify
quality.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "gratisgis_qgis"
HAYSTACK_DIRS = (REPO / "tests", REPO / "scripts")
BASELINE = Path(__file__).resolve().parent / "untested_baseline.txt"


def public_classes(path: Path) -> list[str]:
    """Top-level public class names defined in one module.

    Nested and underscore-prefixed classes are skipped: the former are
    reached through their container, the latter are private by
    convention and testing them directly would pin an implementation
    detail rather than a surface.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]


def haystack() -> str:
    parts = []
    for root in HAYSTACK_DIRS:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue  # this file names every class in the baseline
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def find_untested() -> list[str]:
    """Every ``module::Class`` no test or script mentions, sorted."""
    hay = haystack()
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        for name in public_classes(path):
            if name not in hay:
                found.append(f"{rel}::{name}")
    return sorted(found)


def read_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    lines = BASELINE.read_text(encoding="utf-8").splitlines()
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }


def write_baseline(entries: list[str]) -> None:
    header = (
        "# Public classes with nothing in tests/ or scripts/ naming them.\n"
        "#\n"
        "# This list may only shrink. scripts/untested_audit.py fails the\n"
        "# build when something new appears here, and also when an entry\n"
        "# here has since been covered, so that the debt stays honest\n"
        "# rather than quietly becoming permanent.\n"
        "#\n"
        "# Regenerate with: python scripts/untested_audit.py --update-baseline\n"
    )
    BASELINE.write_text(header + "\n".join(entries) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    current = find_untested()

    if "--update-baseline" in argv:
        write_baseline(current)
        print(f"Baseline rewritten with {len(current)} entries.")
        return 0

    baseline = read_baseline()
    new = [entry for entry in current if entry not in baseline]
    fixed = sorted(baseline - set(current))

    if new:
        print("FAIL: public classes with nothing exercising them.\n")
        for entry in new:
            print(f"  {entry}")
        print(
            "\nBuild one in a test or a smoke check and ask it something. If\n"
            "it genuinely cannot be exercised, add it to\n"
            "scripts/untested_baseline.txt with a comment saying why."
        )
    if fixed:
        if new:
            print()
        print("FAIL: these are covered now and must leave the baseline.\n")
        for entry in fixed:
            print(f"  {entry}")
        print(
            "\nRun: python scripts/untested_audit.py --update-baseline\n"
            "A baseline nobody prunes stops describing the debt."
        )
    if new or fixed:
        return 1

    print(
        f"ok: {len(current)} public classes still untested, all known. "
        f"No new ones."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
