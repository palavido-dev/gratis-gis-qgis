# SPDX-License-Identifier: AGPL-3.0-or-later
"""The last untested surfaces, gathered by what can go wrong.

Four small modules with one thing in common: each is a place where a
failure is silent. A GDAL header scoped too widely leaks a credential
to unrelated servers. A raster resolved to the wrong file publishes
something other than what the user was looking at. A refresh that
quietly does nothing sends the user back to refreshing by hand. And a
task controller nobody can cancel leaves a job running behind a closed
dialog.

None of these raise when they go wrong, which is exactly why none of
them had a test.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.plugin.conftest import ProfileFactory, install_qgis_stub

# -----------------------------------------------------------
# raster_auth: scoping a real credential to one host
# -----------------------------------------------------------


class _Gdal:
    """Records path-scoped option calls the way GDAL 3.6+ takes them."""

    def __init__(self, *, has_path_options: bool = True) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        if has_path_options:
            self.SetPathSpecificOption = self._set

    def _set(self, prefix: str, key: str, value: str | None) -> None:
        self.calls.append((prefix, key, value))


@pytest.fixture
def raster_auth(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {"qgis.PyQt.QtCore": {"QSettings": type("QSettings", (), {})}},
    )
    import gratisgis_qgis.raster_auth as m

    m._configured.clear()
    return m


def _install_gdal(monkeypatch: pytest.MonkeyPatch, gdal: Any) -> None:
    install_qgis_stub(monkeypatch, {"osgeo": {"gdal": gdal}})


class TestRasterAuthScoping:
    def test_the_header_is_scoped_to_the_portals_own_path(
        self,
        raster_auth: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """The value is a real portal API key.

        A global GDAL_HTTP_HEADERS would attach it to every HTTP raster
        the user opens, including other people's servers. The prefix is
        the only thing standing between a credential and that.
        """
        gdal = _Gdal()
        _install_gdal(monkeypatch, gdal)
        monkeypatch.setattr(
            raster_auth, "read_api_header",
            lambda _id: ("Authorization", "Bearer ggk_secret"),
        )
        profile = profile_factory(
            portal_url="https://portal.example", layer_authcfg_id="cfg-1"
        )

        assert raster_auth.configure_gdal_auth(profile) is True
        [(prefix, key, value)] = gdal.calls
        assert prefix.startswith("/vsicurl/https://portal.example")
        assert key == "GDAL_HTTP_HEADERS"
        assert value is not None and "ggk_secret" in value

    def test_an_old_gdal_gets_no_header_rather_than_a_global_one(
        self,
        raster_auth: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """Degrading to the global option would leak the key.

        Private rasters not loading is the better failure, and the
        tempting fallback is the one that must not be added later.
        """
        gdal = _Gdal(has_path_options=False)
        _install_gdal(monkeypatch, gdal)
        monkeypatch.setattr(
            raster_auth, "read_api_header", lambda _id: ("Authorization", "x")
        )
        assert (
            raster_auth.configure_gdal_auth(profile_factory()) is False
        )
        assert gdal.calls == []

    def test_a_signed_out_profile_clears_a_previous_registration(
        self,
        raster_auth: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """Otherwise a stale key keeps being sent for the session.

        This is the mechanism behind a private raster still drawing
        after sign-out: the option outlives the auth database entry.
        """
        gdal = _Gdal()
        _install_gdal(monkeypatch, gdal)
        profile = profile_factory(layer_authcfg_id="cfg-1")

        monkeypatch.setattr(
            raster_auth, "read_api_header", lambda _id: ("Authorization", "x")
        )
        raster_auth.configure_gdal_auth(profile)
        gdal.calls.clear()

        monkeypatch.setattr(raster_auth, "read_api_header", lambda _id: None)
        assert raster_auth.configure_gdal_auth(profile, force=True) is False
        assert gdal.calls and gdal.calls[-1][2] is None, (
            "the stale header must be cleared, not just left alone"
        )

    def test_a_repeat_call_does_not_reread_the_auth_database(
        self,
        raster_auth: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """Every Browser expand calls this.

        Reading the auth database each time can raise a master-password
        prompt, which is the last thing a tree refresh should do.
        """
        _install_gdal(monkeypatch, _Gdal())
        reads: list[str] = []

        def read(authcfg_id: str) -> tuple[str, str]:
            reads.append(authcfg_id)
            return ("Authorization", "x")

        monkeypatch.setattr(raster_auth, "read_api_header", read)
        profile = profile_factory(layer_authcfg_id="cfg-1")
        raster_auth.configure_gdal_auth(profile)
        raster_auth.configure_gdal_auth(profile)
        raster_auth.configure_gdal_auth(profile)
        assert len(reads) == 1

    def test_force_rereads_after_the_key_changed(
        self,
        raster_auth: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        """A re-sign-in mints a new key; the cache is stale by definition."""
        _install_gdal(monkeypatch, _Gdal())
        reads: list[str] = []

        def read(authcfg_id: str) -> tuple[str, str]:
            reads.append(authcfg_id)
            return ("Authorization", "x")

        monkeypatch.setattr(raster_auth, "read_api_header", read)
        profile = profile_factory(layer_authcfg_id="cfg-1")
        raster_auth.configure_gdal_auth(profile)
        raster_auth.configure_gdal_auth(profile, force=True)
        assert len(reads) == 2

    def test_forget_clears_the_option_for_that_portal(
        self,
        raster_auth: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        profile_factory: ProfileFactory,
    ) -> None:
        gdal = _Gdal()
        _install_gdal(monkeypatch, gdal)
        raster_auth.forget(profile_factory(portal_url="https://p.example"))
        assert gdal.calls[-1][2] is None
        assert "p.example" in gdal.calls[-1][0]

    def test_no_gdal_at_all_is_survivable(
        self, raster_auth: ModuleType, profile_factory: ProfileFactory
    ) -> None:
        """A QGIS build without the Python GDAL bindings still works.

        Everything except private raster tile layers, which degrade to
        public-only rather than failing the plugin.
        """
        assert raster_auth.configure_gdal_auth(profile_factory()) is False


# -----------------------------------------------------------
# publish/source: which raster can actually be published
# -----------------------------------------------------------


class TestRasterSourceResolution:
    """Strict on purpose: a path only when the file exists right now."""

    def test_a_real_file_resolves(self, tmp_path: Path) -> None:
        from gratisgis_qgis.publish.source import resolve_raster_source

        tif = tmp_path / "aerial.tif"
        tif.write_bytes(b"II*\x00")
        resolved = resolve_raster_source(str(tif), "gdal")
        assert resolved.file_path == str(tif)
        assert resolved.is_publishable
        assert resolved.reason == ""

    def test_a_streamed_layer_is_refused_with_a_reason(self) -> None:
        """And the reason has to say what to do instead.

        "Cannot publish" alone sends the user looking for a bug.
        """
        from gratisgis_qgis.publish.source import resolve_raster_source

        resolved = resolve_raster_source("type=xyz&url=https://t/{z}", "wms")
        assert not resolved.is_publishable
        assert resolved.reason

    def test_a_path_that_no_longer_exists_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A project whose data moved is the common case.

        Publishing would otherwise fail much later, after the user had
        filled in a title and pressed the button.
        """
        from gratisgis_qgis.publish.source import resolve_raster_source

        resolved = resolve_raster_source(str(tmp_path / "gone.tif"), "gdal")
        assert not resolved.is_publishable
        assert resolved.reason

    def test_an_empty_source_is_refused(self) -> None:
        from gratisgis_qgis.publish.source import resolve_raster_source

        assert not resolve_raster_source("", "").is_publishable


class TestPublishChoice:
    def test_a_choice_with_no_reason_is_publishable(self) -> None:
        from gratisgis_qgis.publish.source import PublishChoice

        assert PublishChoice(
            kind="raster", label="Aerial", file_path="/x.tif"
        ).is_publishable

    def test_a_choice_carrying_a_reason_is_not(self) -> None:
        """The reason IS the flag; two fields would drift apart.

        The picker shows every layer and marks the ones it cannot take,
        so a choice that had a reason and still claimed to be
        publishable would be offered and then fail.
        """
        from gratisgis_qgis.publish.source import PublishChoice

        assert not PublishChoice(
            kind="raster", label="Basemap", reason="It is streamed."
        ).is_publishable


# -----------------------------------------------------------
# browser/refresh: telling the panel that auth changed
# -----------------------------------------------------------


class TestBrowserRefresh:
    def _install(
        self, monkeypatch: pytest.MonkeyPatch, iface: Any
    ) -> ModuleType:
        install_qgis_stub(monkeypatch, {"qgis.utils": {"iface": iface}})
        import gratisgis_qgis.browser.refresh as m

        return m

    def test_only_our_subtree_is_refreshed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full reload walks every provider in the panel.

        That includes filesystem and database connections with nothing
        to do with us, some of which are slow enough to notice.
        """
        calls: list[str] = []
        model = SimpleNamespace(
            refresh=calls.append,
            reload=lambda: calls.append("RELOAD"),
        )
        mod = self._install(
            monkeypatch, SimpleNamespace(browserModel=lambda: model)
        )
        assert mod.refresh_browser_tree() is True
        assert calls == [mod.ROOT_PATH]

    def test_the_refreshed_path_is_the_one_the_root_node_uses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mismatch degrades silently to refreshing nothing.

        Which is exactly the old behaviour, so nobody would notice it
        had stopped working.
        """
        install_qgis_stub(
            monkeypatch,
            {
                "qgis.core": {
                    "QgsDataCollectionItem": type("C", (), {}),
                    "QgsDataItem": type("I", (), {}),
                },
                "qgis.utils": {"iface": None},
            },
        )
        import gratisgis_qgis.browser.refresh as refresh_mod

        # RootItem's path, as items.py constructs it.
        assert refresh_mod.ROOT_PATH == "gratisgis:/"

    def test_a_build_without_refresh_falls_back_to_reload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        model = SimpleNamespace(reload=lambda: calls.append("RELOAD"))
        mod = self._install(
            monkeypatch, SimpleNamespace(browserModel=lambda: model)
        )
        assert mod.refresh_browser_tree() is True
        assert calls == ["RELOAD"]

    def test_a_refresh_that_raises_still_tries_reload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def boom(_path: str) -> None:
            raise RuntimeError("no such path")

        model = SimpleNamespace(
            refresh=boom, reload=lambda: calls.append("RELOAD")
        )
        mod = self._install(
            monkeypatch, SimpleNamespace(browserModel=lambda: model)
        )
        assert mod.refresh_browser_tree() is True
        assert calls == ["RELOAD"]

    @pytest.mark.parametrize(
        "iface",
        [None, SimpleNamespace(browserModel=lambda: None)],
        ids=["headless", "no-model"],
    )
    def test_no_gui_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, iface: Any
    ) -> None:
        """Every caller has already finished a sign-in or a sign-out.

        Failing to redraw a tree must not turn a completed action into
        an error dialog.
        """
        mod = self._install(monkeypatch, iface)
        assert mod.refresh_browser_tree() is False


# -----------------------------------------------------------
# tasks: the handle a dialog cancels through
# -----------------------------------------------------------


class TestTaskController:
    def test_run_in_task_hands_back_something_cancellable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancel is the only way to stop a job behind a closed dialog.

        Both the publish and sync dialogs keep this handle for exactly
        that, and a controller without a cancel would leave a worker
        importing into an item nobody is watching.
        """
        from gratisgis_qgis import tasks

        monkeypatch.setattr(tasks, "_executor", tasks.run_synchronously)
        results: list[Any] = []
        controller = tasks.run_in_task(
            "test", lambda _h: "done", results.append, lambda _e: None
        )
        assert results == ["done"]
        assert hasattr(controller, "cancel")
        # Safe after completion: a dialog closing late calls this
        # without knowing whether the job already finished.
        controller.cancel()

    def test_cancelling_before_the_work_runs_reports_a_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel must not surface as whatever error it interrupted.

        The dialogs tell a cancel apart from a failure by its type, and
        showing "Publish failed" to someone who pressed Cancel is
        reporting their own click back at them as an error.
        """
        from gratisgis_qgis import tasks

        monkeypatch.setattr(tasks, "_executor", tasks.run_synchronously)
        errors: list[BaseException] = []

        def work(handle: Any) -> Any:
            handle.cancel()
            raise RuntimeError("interrupted mid-flight")

        tasks.run_in_task("test", work, lambda _r: None, errors.append)
        assert isinstance(errors[0], tasks.TaskCancelledError)

    def test_a_cancelled_handle_is_visible_to_the_function(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancellation is cooperative; nothing kills a running worker.

        The function has to look, so the handle has to tell the truth.
        """
        from gratisgis_qgis.tasks import InlineTaskHandle

        handle = InlineTaskHandle()
        assert handle.is_canceled() is False
        handle.cancel()
        assert handle.is_canceled() is True
