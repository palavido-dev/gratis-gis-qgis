#!/usr/bin/env python3
"""One-shot rewrite: Qt 5 unscoped enum names -> Qt 6 scoped form.

Both PyQt5 and PyQt6 accept the scoped form, so this is the
cross-version-safe spelling. Idempotent: running twice is a no-op
(the scoped patterns don't match the unscoped regex).

Patterns rewritten:
  Qt.ItemIsSelectable        -> Qt.ItemFlag.ItemIsSelectable
  Qt.UserRole                -> Qt.ItemDataRole.UserRole
  Qt.RightDockWidgetArea     -> Qt.DockWidgetArea.RightDockWidgetArea
  Qt.LeftDockWidgetArea      -> Qt.DockWidgetArea.LeftDockWidgetArea
  Qt.CustomContextMenu       -> Qt.ContextMenuPolicy.CustomContextMenu
  QDialogButtonBox.Ok        -> QDialogButtonBox.StandardButton.Ok
  QDialogButtonBox.Cancel    -> QDialogButtonBox.StandardButton.Cancel
  QMessageBox.Yes            -> QMessageBox.StandardButton.Yes
  QMessageBox.No             -> QMessageBox.StandardButton.No
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


REWRITES = [
    # (regex pattern, replacement). Use word boundaries to avoid
    # matching the scoped names themselves on a second pass.
    (r"\bQt\.ItemIsSelectable\b", "Qt.ItemFlag.ItemIsSelectable"),
    (r"\bQt\.UserRole\b", "Qt.ItemDataRole.UserRole"),
    (r"\bQt\.RightDockWidgetArea\b", "Qt.DockWidgetArea.RightDockWidgetArea"),
    (r"\bQt\.LeftDockWidgetArea\b", "Qt.DockWidgetArea.LeftDockWidgetArea"),
    (r"\bQt\.CustomContextMenu\b", "Qt.ContextMenuPolicy.CustomContextMenu"),
    (r"\bQDialogButtonBox\.Ok\b", "QDialogButtonBox.StandardButton.Ok"),
    (r"\bQDialogButtonBox\.Cancel\b", "QDialogButtonBox.StandardButton.Cancel"),
    (r"\bQMessageBox\.Yes\b", "QMessageBox.StandardButton.Yes"),
    (r"\bQMessageBox\.No\b", "QMessageBox.StandardButton.No"),
]


def main(roots: list[str]) -> int:
    paths: list[Path] = []
    for root in roots:
        p = Path(root)
        if p.is_file():
            paths.append(p)
        else:
            paths.extend(p.rglob("*.py"))

    touched = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        original = text
        for pattern, replacement in REWRITES:
            # Skip a rewrite if the line already contains the
            # scoped form (idempotency). We do this per-line
            # rather than over the whole file because two regions
            # of one file can be in different states.
            new_text = re.sub(pattern, replacement, text)
            text = new_text
        if text != original:
            path.write_text(text, encoding="utf-8")
            touched += 1
            print(f"updated {path}")
    print(f"{touched} file(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["src/gratisgis_qgis"]))
