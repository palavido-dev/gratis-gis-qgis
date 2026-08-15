# SPDX-License-Identifier: AGPL-3.0-or-later
"""Making canvas layers notice that the credential changed.

Reported: signed out, layers stopped drawing; signed back in, the
vector layer came back and the rasters stayed blank.

Clearing the auth manager's cached config fixes what the manager hands
out. It does not reach a provider that already built its request
template, so a layer added before the change keeps whatever it resolved
the first time. The tree behaves and the canvas does not, until QGIS is
restarted.

What is pinned here is which layers get reloaded. Reloading somebody's
PostGIS connection or another organisation's WMS because it happened to
be in the project would be rude and slow, and reloading too few is the
bug itself.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gratisgis_qgis.browser.uris import (
    authed_vector_tile_uri,
    tile_layer_xyz_uri,
    vector_tile_uri,
)
from gratisgis_qgis.layer_reload import layers_using_authcfg

_PORTAL = "https://gratisgis.org"
_CFG = "e53df68"


def _layer(source: str, name: str = "layer") -> Any:
    return SimpleNamespace(source=lambda: source, name=lambda: name)


class TestWhichLayersAreReloaded:
    def test_a_layer_carrying_the_id_is_selected(self) -> None:
        layer = _layer(
            authed_vector_tile_uri(_PORTAL, "item", "lyr", authcfg_id=_CFG)
        )
        assert layers_using_authcfg([layer], _CFG) == [layer]

    def test_a_raster_carrying_the_id_is_selected(self) -> None:
        """The rasters are the layers that actually failed to come back."""
        layer = _layer(tile_layer_xyz_uri(_PORTAL, "item", authcfg_id=_CFG))
        assert layers_using_authcfg([layer], _CFG) == [layer]

    def test_a_reordered_uri_is_still_selected(self) -> None:
        """QGIS rewrites the parameter order on save and reload.

        Matching on position rather than name is the bug that made
        publish-as-map stop recognising a portal raster; the same trap
        applies here, and a layer missed here is a layer that stays
        blank.
        """
        layer = _layer(
            f"authcfg={_CFG}&crs=EPSG%3A3857&type=xyz&url=https%3A%2F%2Fx"
        )
        assert layers_using_authcfg([layer], _CFG) == [layer]

    def test_a_public_portal_layer_is_left_alone(self) -> None:
        """It has no credential, so nothing about it changed."""
        layer = _layer(vector_tile_uri(_PORTAL, "item"))
        assert layers_using_authcfg([layer], _CFG) == []

    def test_another_connection_s_credential_is_left_alone(self) -> None:
        """Two portals can be configured at once.

        Signing out of one must not disturb layers belonging to the
        other.
        """
        layer = _layer(tile_layer_xyz_uri(_PORTAL, "item", authcfg_id="other12"))
        assert layers_using_authcfg([layer], _CFG) == []

    def test_a_layer_that_merely_contains_the_id_is_not_matched(self) -> None:
        """Matching on substring would catch unrelated sources.

        A file path or query string containing the id is not a layer
        authenticating with it.
        """
        layer = _layer(f"/home/matt/{_CFG}/data.gpkg|layername=x")
        assert layers_using_authcfg([layer], _CFG) == []

    def test_an_unrelated_layer_is_left_alone(self) -> None:
        assert layers_using_authcfg(
            [_layer("C:/data/parcels.gpkg|layername=parcels")], _CFG
        ) == []

    def test_a_layer_whose_source_raises_is_skipped(self) -> None:
        """A broken layer must not stop the others being reloaded."""

        def boom() -> str:
            raise RuntimeError("layer is gone")

        good = _layer(tile_layer_xyz_uri(_PORTAL, "item", authcfg_id=_CFG))
        assert layers_using_authcfg(
            [SimpleNamespace(source=boom), good], _CFG
        ) == [good]

    @pytest.mark.parametrize("empty", ["", None])
    def test_no_credential_selects_nothing(self, empty: Any) -> None:
        """A profile with no layer key has no layers to disturb."""
        layer = _layer(tile_layer_xyz_uri(_PORTAL, "item", authcfg_id=_CFG))
        assert layers_using_authcfg([layer], empty or "") == []


class TestReloadIsSafe:
    def test_without_qgis_it_reports_zero(self) -> None:
        """Every caller has already finished a sign-in or sign-out.

        Failing to redraw must not turn a completed action into an
        error dialog.
        """
        from gratisgis_qgis.layer_reload import reload_layers_using

        assert reload_layers_using(_CFG) == 0

    def test_an_empty_id_is_a_no_op(self) -> None:
        from gratisgis_qgis.layer_reload import reload_layers_using

        assert reload_layers_using("") == 0
