# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner rendering in the item properties dialog.

The portal ships ``owner: {id, username, fullName, avatarUrl}`` on both
the item read and the list. The dialog used to read a flat
``ownerUsername`` that the portal has never emitted, so every item
showed a raw UUID as its owner. These pin the nested shape.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub


def _widget(name: str) -> type:
    """A stand-in that accepts any construction and any method call."""
    return type(name, (), {"__init__": lambda self, *a, **k: None})


@pytest.fixture
def owner_label(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], str]:
    # Only the module-level helper is under test, so the Qt names just
    # have to resolve at import time.
    install_qgis_stub(
        monkeypatch,
        {
            # settings.py imports QSettings at module level and the
            # dialog pulls it in transitively.
            "qgis.PyQt.QtCore": {
                "Qt": _widget("Qt"),
                "QSettings": _widget("QSettings"),
            },
            "qgis.PyQt.QtWidgets": {
                n: _widget(n)
                for n in (
                    "QDialog",
                    "QFormLayout",
                    "QHBoxLayout",
                    "QLabel",
                    "QPushButton",
                    "QTextEdit",
                    "QVBoxLayout",
                    "QWidget",
                )
            },
        },
    )
    from gratisgis_qgis.ui.item_properties_dialog import _owner_label

    return _owner_label


# The exact payload shape the live portal returns.
_REAL = {
    "ownerId": "e39beba6-aa8d-4a29-98bd-d248edf8258a",
    "owner": {
        "id": "e39beba6-aa8d-4a29-98bd-d248edf8258a",
        "username": "admin",
        "fullName": "Site Admin",
        "avatarUrl": None,
    },
}


def test_prefers_full_name_with_username(owner_label: Callable[[dict[str, Any]], str]) -> None:
    assert owner_label(_REAL) == "Site Admin (admin)"
    # The regression: a UUID must never be what the user reads.
    assert "e39beba6" not in owner_label(_REAL)


def test_username_only_when_no_full_name(owner_label: Callable[[dict[str, Any]], str]) -> None:
    payload = {"ownerId": "uuid-1", "owner": {"username": "matt", "fullName": None}}
    assert owner_label(payload) == "matt"


def test_falls_back_to_id_when_owner_is_missing(owner_label: Callable[[dict[str, Any]], str]) -> None:
    # An owner whose account is gone still has to render something.
    assert owner_label({"ownerId": "uuid-1"}) == "uuid-1"
    assert owner_label({"ownerId": "uuid-1", "owner": None}) == "uuid-1"


def test_empty_payload_renders_empty(owner_label: Callable[[dict[str, Any]], str]) -> None:
    assert owner_label({}) == ""
