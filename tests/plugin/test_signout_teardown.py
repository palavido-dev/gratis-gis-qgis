# SPDX-License-Identifier: AGPL-3.0-or-later
"""What signing out has to leave behind, and what it must not.

Reported from a real session: adding a private layer after signing out
produced "FAILED to load config <id> from any storage". Sign-out
deleted the authcfg entry, but the id is written into every layer URI
the connection ever built, and those URIs outlive it in saved projects
and in layers already on the canvas. The entry is now emptied rather
than deleted, so the lookup still resolves and the layer fails with an
ordinary 401.

A second bug lived here and no longer can. A private raster kept
drawing after sign-out because its credential was a process-wide GDAL
option rather than an authcfg, and clearing the auth database did not
reach it. Raster layers no longer go through GDAL at all, so there is
no second credential store left to forget.

These run against the real ``_signed_out`` with its collaborators
stubbed at module level, so the assertions are about which calls the
production function actually makes.
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.plugin.conftest import ProfileFactory, install_qgis_stub


@pytest.fixture
def dialog_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``connection_dialog`` importable without Qt widgets."""
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.PyQt.QtCore": {
                "QSettings": type("QSettings", (), {}),
                "Qt": type("Qt", (), {}),
                "QTimer": type("QTimer", (), {}),
            },
            # Exactly the widget names connection_dialog imports at
            # module level. Listed rather than guessed generously: a
            # name that quietly stops being imported should show up as
            # an unused stub, not hide behind a pile of spares.
            "qgis.PyQt.QtWidgets": {
                name: type(name, (), {})
                for name in (
                    "QApplication", "QCheckBox", "QDialog",
                    "QDialogButtonBox", "QFormLayout", "QHBoxLayout",
                    "QLabel", "QLineEdit", "QListWidget", "QMessageBox",
                    "QPushButton", "QVBoxLayout", "QWidget",
                )
            },
            "qgis.PyQt.QtGui": {"QIcon": type("QIcon", (), {})},
            "qgis.core": {
                "QgsApplication": type("QgsApplication", (), {}),
                "QgsAuthMethodConfig": type("QgsAuthMethodConfig", (), {}),
            },
            "qgis.utils": {"iface": None},
        },
    )
    import gratisgis_qgis.ui.connection_dialog as mod

    return mod


class _Spy:
    """Records calls and returns a configurable result."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.result

    @property
    def called(self) -> bool:
        return bool(self.calls)


@pytest.fixture
def spies(dialog_mod: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, _Spy]:
    made = {
        "clear_api_header_credential": _Spy(result=True),
        "remove_authcfg": _Spy(),
    }
    for name, spy in made.items():
        monkeypatch.setattr(dialog_mod, name, spy)
    return made


class TestSignedOutProfile:
    def test_the_authcfg_is_emptied_not_deleted(
        self,
        dialog_mod: Any,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """Deleting it strands every layer and project that names it."""
        profile = profile_factory(
            authcfg_id="auth-1", api_key_id="key-1", layer_authcfg_id="lay-1"
        )
        dialog_mod._signed_out(profile)
        assert spies["clear_api_header_credential"].called
        assert not spies["remove_authcfg"].called, (
            "removing the entry is what produced 'FAILED to load config'"
        )

    def test_the_layer_authcfg_id_survives_sign_out(
        self,
        dialog_mod: Any,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """So the next sign-in revives existing layers.

        ``_apply_layer_key`` reuses ``layer_authcfg_id`` when it is set.
        Clearing it here would mint a fresh id instead, and every layer
        and project pointing at the old one would be orphaned for good
        rather than just until the user signs back in.
        """
        profile = profile_factory(
            authcfg_id="auth-1", api_key_id="key-1", layer_authcfg_id="lay-1"
        )
        out = dialog_mod._signed_out(profile)
        assert out.layer_authcfg_id == "lay-1"

    def test_the_session_is_actually_ended(
        self,
        dialog_mod: Any,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """Keeping the layer entry must not keep the user signed in.

        ``authcfg_id`` is what the Browser tree and every portal call
        gate on. If preserving the layer credential also preserved this,
        sign-out would be cosmetic.
        """
        profile = profile_factory(
            authcfg_id="auth-1", api_key_id="key-1", layer_authcfg_id="lay-1"
        )
        out = dialog_mod._signed_out(profile)
        assert out.authcfg_id == ""
        assert out.api_key_id == ""

    def test_a_failed_empty_falls_back_to_deleting(
        self,
        dialog_mod: Any,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """A live credential left behind is worse than a dead reference.

        If the entry cannot be rewritten, it still holds a working key
        for a user who just asked to be signed out. Delete it, and drop
        the id too so nothing is left pointing at something gone.
        """
        spies["clear_api_header_credential"].result = False
        profile = profile_factory(
            authcfg_id="auth-1", api_key_id="key-1", layer_authcfg_id="lay-1"
        )
        out = dialog_mod._signed_out(profile)
        assert spies["remove_authcfg"].called
        assert out.layer_authcfg_id == ""
