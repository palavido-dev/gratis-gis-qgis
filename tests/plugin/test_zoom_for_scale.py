# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turning a QGIS scale denominator into a tile zoom (issue #27).

A published map opened at the wrong extent. The zoom was derived as
``log2(559082264 / scale)``, using the Web Mercator scale denominator
at the EQUATOR, with no correction for latitude. Mercator scale grows
with 1/cos(latitude), so the result was wrong everywhere except the
equator, and wrong by a consistent amount in a consistent direction:
about 0.36 levels too far in at Randolph County.

The reference values below are computed from the projection's own
definition rather than copied from a previous run of this code, so
they check the formula rather than agreeing with it:

    scale = SCALE_Z0 * cos(latitude) / 2**zoom
"""
from __future__ import annotations

import math

import pytest

from gratisgis_qgis.publish.project_to_map import (
    MAX_ZOOM,
    MERCATOR_MAX_LAT,
    MIN_ZOOM,
    WEB_MERCATOR_SCALE_Z0,
    zoom_for_scale,
)

#: Randolph County, WV. The project the bug was found on.
RANDOLPH_LAT = 38.9


def scale_at(zoom: float, latitude_deg: float) -> float:
    """The scale denominator Web Mercator gives at this zoom and place."""
    return float(
        WEB_MERCATOR_SCALE_Z0
        * math.cos(math.radians(latitude_deg))
        / (2.0**zoom)
    )


class TestRoundTrip:
    """Every zoom must survive being turned into a scale and back."""

    @pytest.mark.parametrize("zoom", [0, 1, 5, 10, 12, 14, 18, 21])
    @pytest.mark.parametrize(
        "lat", [0.0, 38.9, 51.5, -33.9, 64.1], ids=[
            "equator", "randolph", "london", "sydney", "reykjavik"
        ]
    )
    def test_scale_to_zoom_and_back(self, zoom: float, lat: float) -> None:
        assert zoom_for_scale(scale_at(zoom, lat), lat) == pytest.approx(
            zoom, abs=1e-9
        )


class TestTheBug:
    def test_the_equator_formula_was_wrong_away_from_the_equator(self) -> None:
        """The regression, stated as the size of the error it caused.

        The old code returned log2(SCALE_Z0 / scale). At Randolph
        County that is log2(1/cos(38.9)) too high, which is 0.36 of a
        zoom level, every time, always zoomed in too far.
        """
        scale = scale_at(12.0, RANDOLPH_LAT)
        old = math.log2(WEB_MERCATOR_SCALE_Z0 / scale)
        new = zoom_for_scale(scale, RANDOLPH_LAT)

        assert new == pytest.approx(12.0)
        assert old == pytest.approx(12.36, abs=0.01)
        assert old > new, "the old result was always too far in"

    def test_the_two_agree_on_the_equator(self) -> None:
        """Which is why it went unnoticed in any test at lat 0."""
        scale = scale_at(10.0, 0.0)
        assert zoom_for_scale(scale, 0.0) == pytest.approx(
            math.log2(WEB_MERCATOR_SCALE_Z0 / scale)
        )

    def test_the_error_grows_with_latitude(self) -> None:
        """A user further north gets a worse answer.

        Worth pinning as a property rather than one number, because it
        is the reason the symptom was reported as "sometimes a bit off"
        rather than as a constant offset.
        """
        scale = scale_at(12.0, 0.0)
        errors = [
            abs(zoom_for_scale(scale, lat) - 12.0)
            for lat in (0.0, 30.0, 60.0)
        ]
        assert errors == sorted(errors)
        assert errors[0] == pytest.approx(0.0)


class TestDegenerateInputs:
    @pytest.mark.parametrize("scale", [0.0, -1.0, -1e9])
    def test_a_nonpositive_scale_is_the_minimum_zoom(
        self, scale: float
    ) -> None:
        """log2 of zero or a negative is a crash on Publish."""
        assert zoom_for_scale(scale, RANDOLPH_LAT) == MIN_ZOOM

    @pytest.mark.parametrize(
        "lat", [91.0, -91.0, 1e6, float("nan")],
        ids=["just-past-pole", "just-past-south", "wrapped", "nan"],
    )
    def test_a_value_that_is_not_a_latitude_gives_no_answer(
        self, lat: float
    ) -> None:
        """Range-checked, not left to cos to reject.

        cos is periodic, so 1e6 degrees comes back as a perfectly
        ordinary cosine and produces a confident, meaningless zoom.
        Found by this test: an earlier version guarded only on
        ``cos(lat) <= 0`` and sailed straight through it.
        """
        assert zoom_for_scale(1000.0, lat) == MIN_ZOOM

    @pytest.mark.parametrize("lat", [90.0, -90.0, 88.0, -88.0])
    def test_a_real_latitude_past_the_grid_edge_is_clamped_not_dropped(
        self, lat: float
    ) -> None:
        """A canvas can legitimately sit there; give the grid's edge.

        Reachable from a project in a polar CRS. Returning zoom 0 for
        it would publish a whole-world view of somebody's Arctic
        survey, which looks like data loss rather than a rounding.
        """
        at_edge = zoom_for_scale(1000.0, MERCATOR_MAX_LAT)
        assert zoom_for_scale(1000.0, lat) == pytest.approx(at_edge)
        assert at_edge > MIN_ZOOM

    def test_a_hugely_zoomed_in_canvas_is_clamped(self) -> None:
        """The portal's viewer has no zoom 30."""
        assert zoom_for_scale(1e-9, RANDOLPH_LAT) == MAX_ZOOM

    def test_a_whole_planet_view_is_clamped_at_the_bottom(self) -> None:
        assert zoom_for_scale(1e12, RANDOLPH_LAT) == MIN_ZOOM

    def test_the_result_is_always_within_the_portal_range(self) -> None:
        for scale in (1e-6, 1.0, 1e3, 1e6, 1e15):
            for lat in (-89.0, 0.0, 45.0, 89.0):
                assert MIN_ZOOM <= zoom_for_scale(scale, lat) <= MAX_ZOOM
