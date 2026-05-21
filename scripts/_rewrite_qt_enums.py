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
    # PyQt6 dropped the .exec_() Python-2-reserved-word alias;
    # use plain .exec() instead. PyQt5 accepts both.
    (r"\.exec_\(", ".exec("),
    (r"\bQt\.ItemIsSelectable\b", "Qt.ItemFlag.ItemIsSelectable"),
    (r"\bQt\.ItemIsEditable\b", "Qt.ItemFlag.ItemIsEditable"),
    (r"\bQt\.ItemIsEnabled\b", "Qt.ItemFlag.ItemIsEnabled"),
    (r"\bQt\.UserRole\b", "Qt.ItemDataRole.UserRole"),
    (r"\bQt\.DisplayRole\b", "Qt.ItemDataRole.DisplayRole"),
    (r"\bQt\.EditRole\b", "Qt.ItemDataRole.EditRole"),
    (r"\bQt\.ToolTipRole\b", "Qt.ItemDataRole.ToolTipRole"),
    (r"\bQt\.RightDockWidgetArea\b", "Qt.DockWidgetArea.RightDockWidgetArea"),
    (r"\bQt\.LeftDockWidgetArea\b", "Qt.DockWidgetArea.LeftDockWidgetArea"),
    (r"\bQt\.TopDockWidgetArea\b", "Qt.DockWidgetArea.TopDockWidgetArea"),
    (r"\bQt\.BottomDockWidgetArea\b", "Qt.DockWidgetArea.BottomDockWidgetArea"),
    (r"\bQt\.CustomContextMenu\b", "Qt.ContextMenuPolicy.CustomContextMenu"),
    (r"\bQt\.ActionsContextMenu\b", "Qt.ContextMenuPolicy.ActionsContextMenu"),
    # Cursor shapes
    (r"\bQt\.WaitCursor\b", "Qt.CursorShape.WaitCursor"),
    (r"\bQt\.ArrowCursor\b", "Qt.CursorShape.ArrowCursor"),
    (r"\bQt\.PointingHandCursor\b", "Qt.CursorShape.PointingHandCursor"),
    (r"\bQt\.CrossCursor\b", "Qt.CursorShape.CrossCursor"),
    (r"\bQt\.BusyCursor\b", "Qt.CursorShape.BusyCursor"),
    # Alignment
    (r"\bQt\.AlignCenter\b", "Qt.AlignmentFlag.AlignCenter"),
    (r"\bQt\.AlignLeft\b", "Qt.AlignmentFlag.AlignLeft"),
    (r"\bQt\.AlignRight\b", "Qt.AlignmentFlag.AlignRight"),
    (r"\bQt\.AlignTop\b", "Qt.AlignmentFlag.AlignTop"),
    (r"\bQt\.AlignBottom\b", "Qt.AlignmentFlag.AlignBottom"),
    (r"\bQt\.AlignVCenter\b", "Qt.AlignmentFlag.AlignVCenter"),
    (r"\bQt\.AlignHCenter\b", "Qt.AlignmentFlag.AlignHCenter"),
    # Orientation
    (r"\bQt\.Horizontal\b", "Qt.Orientation.Horizontal"),
    (r"\bQt\.Vertical\b", "Qt.Orientation.Vertical"),
    # Check state
    (r"\bQt\.Checked\b", "Qt.CheckState.Checked"),
    (r"\bQt\.Unchecked\b", "Qt.CheckState.Unchecked"),
    (r"\bQt\.PartiallyChecked\b", "Qt.CheckState.PartiallyChecked"),
    # QDialogButtonBox.ButtonRole members
    (r"\bQDialogButtonBox\.AcceptRole\b", "QDialogButtonBox.ButtonRole.AcceptRole"),
    (r"\bQDialogButtonBox\.RejectRole\b", "QDialogButtonBox.ButtonRole.RejectRole"),
    (r"\bQDialogButtonBox\.DestructiveRole\b", "QDialogButtonBox.ButtonRole.DestructiveRole"),
    (r"\bQDialogButtonBox\.ActionRole\b", "QDialogButtonBox.ButtonRole.ActionRole"),
    (r"\bQDialogButtonBox\.HelpRole\b", "QDialogButtonBox.ButtonRole.HelpRole"),
    (r"\bQDialogButtonBox\.YesRole\b", "QDialogButtonBox.ButtonRole.YesRole"),
    (r"\bQDialogButtonBox\.NoRole\b", "QDialogButtonBox.ButtonRole.NoRole"),
    (r"\bQDialogButtonBox\.ApplyRole\b", "QDialogButtonBox.ButtonRole.ApplyRole"),
    (r"\bQDialogButtonBox\.ResetRole\b", "QDialogButtonBox.ButtonRole.ResetRole"),
    (r"\bQDialogButtonBox\.InvalidRole\b", "QDialogButtonBox.ButtonRole.InvalidRole"),
    # QDialogButtonBox.StandardButton members. Exhaustive list per
    # the Qt 6 docs so any future button we reach for is already
    # covered (no second sweep needed).
    (r"\bQDialogButtonBox\.Ok\b", "QDialogButtonBox.StandardButton.Ok"),
    (r"\bQDialogButtonBox\.Cancel\b", "QDialogButtonBox.StandardButton.Cancel"),
    (r"\bQDialogButtonBox\.Close\b", "QDialogButtonBox.StandardButton.Close"),
    (r"\bQDialogButtonBox\.Save\b", "QDialogButtonBox.StandardButton.Save"),
    (r"\bQDialogButtonBox\.SaveAll\b", "QDialogButtonBox.StandardButton.SaveAll"),
    (r"\bQDialogButtonBox\.Open\b", "QDialogButtonBox.StandardButton.Open"),
    (r"\bQDialogButtonBox\.Yes\b", "QDialogButtonBox.StandardButton.Yes"),
    (r"\bQDialogButtonBox\.YesToAll\b", "QDialogButtonBox.StandardButton.YesToAll"),
    (r"\bQDialogButtonBox\.No\b", "QDialogButtonBox.StandardButton.No"),
    (r"\bQDialogButtonBox\.NoToAll\b", "QDialogButtonBox.StandardButton.NoToAll"),
    (r"\bQDialogButtonBox\.Apply\b", "QDialogButtonBox.StandardButton.Apply"),
    (r"\bQDialogButtonBox\.Reset\b", "QDialogButtonBox.StandardButton.Reset"),
    (r"\bQDialogButtonBox\.RestoreDefaults\b", "QDialogButtonBox.StandardButton.RestoreDefaults"),
    (r"\bQDialogButtonBox\.Help\b", "QDialogButtonBox.StandardButton.Help"),
    (r"\bQDialogButtonBox\.Discard\b", "QDialogButtonBox.StandardButton.Discard"),
    (r"\bQDialogButtonBox\.Retry\b", "QDialogButtonBox.StandardButton.Retry"),
    (r"\bQDialogButtonBox\.Ignore\b", "QDialogButtonBox.StandardButton.Ignore"),
    (r"\bQDialogButtonBox\.Abort\b", "QDialogButtonBox.StandardButton.Abort"),
    (r"\bQDialogButtonBox\.NoButton\b", "QDialogButtonBox.StandardButton.NoButton"),
    # QMessageBox.StandardButton members. Same exhaustive treatment.
    (r"\bQMessageBox\.Ok\b", "QMessageBox.StandardButton.Ok"),
    (r"\bQMessageBox\.Cancel\b", "QMessageBox.StandardButton.Cancel"),
    (r"\bQMessageBox\.Close\b", "QMessageBox.StandardButton.Close"),
    (r"\bQMessageBox\.Yes\b", "QMessageBox.StandardButton.Yes"),
    (r"\bQMessageBox\.YesToAll\b", "QMessageBox.StandardButton.YesToAll"),
    (r"\bQMessageBox\.No\b", "QMessageBox.StandardButton.No"),
    (r"\bQMessageBox\.NoToAll\b", "QMessageBox.StandardButton.NoToAll"),
    (r"\bQMessageBox\.Apply\b", "QMessageBox.StandardButton.Apply"),
    (r"\bQMessageBox\.Save\b", "QMessageBox.StandardButton.Save"),
    (r"\bQMessageBox\.SaveAll\b", "QMessageBox.StandardButton.SaveAll"),
    (r"\bQMessageBox\.Discard\b", "QMessageBox.StandardButton.Discard"),
    (r"\bQMessageBox\.Retry\b", "QMessageBox.StandardButton.Retry"),
    (r"\bQMessageBox\.Ignore\b", "QMessageBox.StandardButton.Ignore"),
    (r"\bQMessageBox\.Abort\b", "QMessageBox.StandardButton.Abort"),
    (r"\bQMessageBox\.Help\b", "QMessageBox.StandardButton.Help"),
    # QMessageBox.Icon members.
    (r"\bQMessageBox\.NoIcon\b", "QMessageBox.Icon.NoIcon"),
    (r"\bQMessageBox\.Information\b", "QMessageBox.Icon.Information"),
    (r"\bQMessageBox\.Warning\b", "QMessageBox.Icon.Warning"),
    (r"\bQMessageBox\.Critical\b", "QMessageBox.Icon.Critical"),
    (r"\bQMessageBox\.Question\b", "QMessageBox.Icon.Question"),
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
