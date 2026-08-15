# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rendering one portal item's metadata (Test 2 in the playbook).

Read-only, so nothing here can damage anything, which is exactly why it
never got tested. What it can do is show the wrong thing confidently: an
owner rendered as a raw UUID, an absent field rendered as the string
"None", or a missing item rendered as a blank dialog with no
explanation.

The owner is the one with history. It used to read a flat
``ownerUsername`` the portal has never sent, so every item fell through
to the UUID and nobody noticed, because a UUID in an owner field looks
like data rather than a bug.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub

_WIDGETS = [
    "QDialog", "QFormLayout", "QHBoxLayout", "QLabel", "QPushButton",
    "QTextEdit", "QVBoxLayout", "QWidget",
]


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": {n: type(n, (), {}) for n in _WIDGETS},
        },
    )
    import gratisgis_qgis.ui.item_properties_dialog as m

    return m


class _Field:
    """A label or text box, remembering whatever was last set on it."""

    def __init__(self) -> None:
        self.text = ""

    def setText(self, value: str) -> None:  # Qt API name
        self.text = value

    def setPlainText(self, value: str) -> None:  # Qt API name
        self.text = value


def _dialog(mod: ModuleType) -> Any:
    dlg = mod.ItemPropertiesDialog.__new__(mod.ItemPropertiesDialog)
    for name in (
        "_title", "_type_row", "_description", "_tags",
        "_access", "_owner", "_created", "_updated",
    ):
        setattr(dlg, name, _Field())
    return dlg


class TestOwnerLabel:
    def test_a_full_name_and_username_read_as_a_person(
        self, mod: ModuleType
    ) -> None:
        assert mod._owner_label(
            {"owner": {"username": "mpalavido", "fullName": "Matt Palavido"}}
        ) == "Matt Palavido (mpalavido)"

    def test_a_username_alone_is_enough(self, mod: ModuleType) -> None:
        assert mod._owner_label({"owner": {"username": "mpalavido"}}) == (
            "mpalavido"
        )

    def test_a_full_name_alone_is_enough(self, mod: ModuleType) -> None:
        assert mod._owner_label({"owner": {"fullName": "Matt Palavido"}}) == (
            "Matt Palavido"
        )

    def test_the_uuid_is_the_last_resort_not_the_first(
        self, mod: ModuleType
    ) -> None:
        """The bug this function was rewritten for.

        It read a key the portal has never sent, so every item fell
        through to here and showed a UUID, which looks like data rather
        than a failure to resolve a name.
        """
        assert mod._owner_label({"ownerId": "8b1f-0000"}) == "8b1f-0000"

    def test_an_owner_whose_account_is_gone_still_renders(
        self, mod: ModuleType
    ) -> None:
        assert mod._owner_label(
            {"owner": None, "ownerId": "8b1f-0000"}
        ) == "8b1f-0000"

    def test_nothing_at_all_renders_as_empty_not_none(
        self, mod: ModuleType
    ) -> None:
        """The string "None" in an owner field is a classic tell."""
        assert mod._owner_label({}) == ""


class TestRenderItem:
    def test_a_full_item_fills_every_field(self, mod: ModuleType) -> None:
        dlg = _dialog(mod)
        mod._render_item(
            {
                "title": "Parcels",
                "type": "data_layer",
                "access": "org",
                "description": "County parcels",
                "tags": ["parcels", "wv"],
                "createdAt": "2026-01-02T03:04:05Z",
                "updatedAt": "2026-08-15T00:00:00Z",
                "owner": {"username": "mpalavido", "fullName": "Matt"},
            },
            dlg,
        )
        assert dlg._title.text == "Parcels"
        assert "data_layer" in dlg._type_row.text
        assert dlg._description.text == "County parcels"
        assert dlg._tags.text == "parcels, wv"
        assert dlg._access.text == "org"
        assert dlg._owner.text == "Matt (mpalavido)"
        assert dlg._created.text.startswith("2026-01-02")

    def test_missing_fields_read_as_words_not_as_none(
        self, mod: ModuleType
    ) -> None:
        """Every absent value gets a human placeholder.

        A dialog reading "None" four times looks broken, and a user
        cannot tell "the portal did not send this" from "the plugin
        failed to read it".
        """
        dlg = _dialog(mod)
        mod._render_item({}, dlg)
        assert "None" not in dlg._title.text
        assert "None" not in dlg._type_row.text
        assert "None" not in dlg._access.text
        assert dlg._tags.text == "(none)"
        assert dlg._description.text == ""

    def test_an_untitled_item_says_so_rather_than_showing_blank(
        self, mod: ModuleType
    ) -> None:
        dlg = _dialog(mod)
        mod._render_item({"type": "map"}, dlg)
        assert dlg._title.text == "(untitled)"

    def test_an_empty_tag_list_is_not_an_empty_string(
        self, mod: ModuleType
    ) -> None:
        """A blank row reads as a rendering failure, not as "no tags"."""
        dlg = _dialog(mod)
        mod._render_item({"tags": []}, dlg)
        assert dlg._tags.text == "(none)"


class TestNotFound:
    def test_a_missing_item_explains_the_likely_reasons(
        self, mod: ModuleType
    ) -> None:
        """The three causes are indistinguishable from here.

        Private, deleted, or an expired session all return the same
        nothing, so the message names all three rather than guessing
        and sending the user to fix the wrong one.
        """
        dlg = _dialog(mod)
        dlg._show_not_found()
        assert "not found" in dlg._title.text.lower()
        message = dlg._type_row.text.lower()
        assert "private" in message
        assert "deleted" in message
        assert "expired" in message


class TestPopulate:
    def test_the_fetch_runs_off_the_gui_thread(
        self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opening properties on a dead portal must not freeze QGIS.

        The whole dialog exists to show one HTTP response, so it is the
        easiest place in the plugin to accidentally block the window.
        """
        scheduled: list[Any] = []
        monkeypatch.setattr(
            mod, "run_in_task", lambda *a, **k: scheduled.append(a)
        )
        monkeypatch.setattr(
            mod, "get_item", lambda *_a: pytest.fail("fetched on the GUI thread")
        )
        dlg = _dialog(mod)
        dlg._profile = SimpleNamespace(name="demo")
        dlg._item_id = "item-1"
        dlg._populate()
        assert scheduled, "the fetch must be scheduled, not run inline"
