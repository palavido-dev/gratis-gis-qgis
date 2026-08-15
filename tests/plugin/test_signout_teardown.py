# SPDX-License-Identifier: AGPL-3.0-or-later
"""What signing out has to leave behind, and what it must not.

Two bugs sat here at once, both reported from a real session.

A private raster kept drawing after sign-out. Raster tile_layers do not
read the authcfg at all: GDAL cannot use one, so the credential is
installed as a process-wide GDAL option instead, and clearing the auth
database does nothing to it. Sign-out hand-rolled its teardown and
never called ``raster_forget``, so the portal key stayed registered for
the life of the QGIS process.

Adding a private layer afterwards produced "FAILED to load config
<id> from any storage". Sign-out deleted the authcfg entry, but the id
is written into every layer URI the connection ever built, and those
URIs outlive it in saved projects and in layers already on the canvas.
The entry is now emptied rather than deleted, so the lookup still
resolves and the layer fails with an ordinary 401.

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
        "raster_forget": _Spy(),
        "reload_layers_using": _Spy(result=0),
    }
    for name, spy in made.items():
        monkeypatch.setattr(dialog_mod, name, spy)
    return made


class TestSignedOutProfile:
    def test_the_gdal_header_is_always_forgotten(
        self,
        dialog_mod: Any,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """The bug that let a private raster keep drawing.

        Nothing else in the teardown touches the GDAL option, and it
        outlives the auth database entry, so if this call is missing the
        credential simply stays installed until QGIS exits.
        """
        profile = profile_factory(
            authcfg_id="auth-1", api_key_id="key-1", layer_authcfg_id="lay-1"
        )
        dialog_mod._signed_out(profile)
        assert spies["raster_forget"].called

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

    def test_layers_on_the_canvas_are_reloaded(
        self,
        dialog_mod: Any,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """The third way a signed-out layer keeps drawing.

        Emptying the credential changes what the auth manager hands out
        from here on. It does not reach a provider that already built
        its request template when the layer was added, so that layer
        carries on drawing with a credential the user just revoked.
        Reported as "I signed out, but all my existing map layers still
        draw from the portal".
        """
        profile = profile_factory(
            authcfg_id="auth-1", api_key_id="key-1", layer_authcfg_id="lay-1"
        )
        dialog_mod._signed_out(profile)
        assert spies["reload_layers_using"].calls == [(("lay-1",), {})]

    def test_the_reload_happens_after_the_credential_is_emptied(
        self,
        dialog_mod: Any,
        monkeypatch: pytest.MonkeyPatch,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """Order is the whole point.

        Reloading first would have every provider re-resolve the key
        that is about to be cleared, which is precisely the state being
        fixed, and it would look like it worked.
        """
        order: list[str] = []
        for name in ("clear_api_header_credential", "raster_forget",
                     "reload_layers_using"):
            def record(*_a: Any, _n: str = name, **_k: Any) -> Any:
                order.append(_n)
                return True if _n == "clear_api_header_credential" else None

            monkeypatch.setattr(dialog_mod, name, record)
        dialog_mod._signed_out(
            profile_factory(
                authcfg_id="auth-1", api_key_id="key-1",
                layer_authcfg_id="lay-1",
            )
        )
        assert order.index("reload_layers_using") > order.index(
            "clear_api_header_credential"
        )
        assert order.index("reload_layers_using") > order.index(
            "raster_forget"
        )

    def test_the_fallback_path_reloads_too(
        self,
        dialog_mod: Any,
        spies: dict[str, _Spy],
        profile_factory: ProfileFactory,
    ) -> None:
        """The branch that deletes the entry instead of emptying it.

        It is the rarer branch, so it is the one that gets forgotten,
        and it is also the branch where the layer is most broken.
        """
        spies["clear_api_header_credential"].result = False
        profile = profile_factory(
            authcfg_id="auth-1", api_key_id="key-1", layer_authcfg_id="lay-1"
        )
        dialog_mod._signed_out(profile)
        assert spies["reload_layers_using"].calls == [(("lay-1",), {})]

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
        assert spies["raster_forget"].called
        assert out.layer_authcfg_id == ""
