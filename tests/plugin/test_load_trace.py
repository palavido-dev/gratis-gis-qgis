# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trail project load leaves behind.

The freeze under investigation produced a silent log, so these pin the
two facts that would have made it diagnosable: which layer was being
added, and whether the layer carried an ``authcfg`` into a locked auth
database.

Sources here are built by the real URI builders rather than written out
by hand. A hand-written string is a guess about what the builders emit,
and a test that asserts against a guess keeps passing after the
builders change, which is exactly how the clone bugs stayed green
through two releases.
"""
from __future__ import annotations

from gratisgis_qgis.browser.uris import (
    authed_vector_tile_uri,
    oapif_uri,
    tile_layer_cog_uri,
    tile_layer_xyz_uri,
    vector_tile_uri,
)
from gratisgis_qgis.load_trace import authcfg_id_in, describe_layer

_PORTAL = "https://portal.example"
_ITEM = "8b1f0f4e-0000-4000-8000-000000000001"
_LAYER = "roads"
_AUTHCFG = "ab12cd3"


class TestAuthcfgIdIn:
    """Recovering the authcfg id a layer references.

    This is the id that a signed-out session has deleted from the auth
    database while every saved project still points at it, which is the
    dangling reference the freeze is suspected to start from. Logging it
    is what lets that be recognised on sight.
    """

    def test_finds_the_id_in_an_authed_vector_tile_source(self) -> None:
        source = authed_vector_tile_uri(
            _PORTAL, _ITEM, _LAYER, authcfg_id=_AUTHCFG
        )
        assert authcfg_id_in(source) == _AUTHCFG

    def test_finds_the_id_in_an_authed_raster_xyz_source(self) -> None:
        source = tile_layer_xyz_uri(_PORTAL, _ITEM, authcfg_id=_AUTHCFG)
        assert authcfg_id_in(source) == _AUTHCFG

    def test_quoted_spelling_is_recognised(self) -> None:
        """QGIS writes key='value' for some providers.

        Both spellings reach this function, because it is handed
        whatever ``layer.source()`` returns rather than what our
        builders emitted.
        """
        assert authcfg_id_in("url='x' authcfg='ab12cd3'") == _AUTHCFG

    def test_public_sources_have_no_id(self) -> None:
        assert authcfg_id_in(vector_tile_uri(_PORTAL, _ITEM)) == ""
        assert authcfg_id_in(oapif_uri(_PORTAL, _ITEM)) == ""
        assert authcfg_id_in(tile_layer_cog_uri(_PORTAL, _ITEM)) == ""

    def test_empty_source_is_not_an_error(self) -> None:
        assert authcfg_id_in("") == ""


class TestDescribeLayer:
    """One log line per layer, saying enough to name a suspect."""

    def test_an_authed_portal_layer_is_marked_as_both(self) -> None:
        source = authed_vector_tile_uri(
            _PORTAL, _ITEM, _LAYER, authcfg_id=_AUTHCFG
        )
        line = describe_layer("Roads", source)
        assert "Roads" in line
        assert "portal" in line
        assert f"authcfg={_AUTHCFG}" in line

    def test_a_public_portal_layer_says_it_has_no_authcfg(self) -> None:
        """The distinction is the whole diagnostic.

        Portal layers that carry no authcfg cannot reach the auth
        manager, so a freeze that happens with only these on the canvas
        would rule the leading theory out rather than confirm it.
        """
        line = describe_layer("Parcels", vector_tile_uri(_PORTAL, _ITEM))
        assert "portal" in line
        assert "no authcfg" in line

    def test_an_unrelated_layer_is_not_claimed_as_ours(self) -> None:
        """This fires for every layer in the project, not just ours.

        A trail that labelled the user's shapefiles as portal layers
        would point the next investigation at the wrong half of the
        project.
        """
        line = describe_layer("Field notes", "/home/matt/notes.gpkg|layername=notes")
        assert "other" in line
        assert "portal" not in line.replace("other", "")

    def test_a_long_source_is_truncated(self) -> None:
        """Sources are long, and are the one place a key could appear.

        No builder puts a credential in a URI today. Truncating means a
        future one that does cannot spill it into a log file the user is
        about to attach to a public issue.
        """
        line = describe_layer("Big", "type=xyz&url=" + "x" * 5000)
        assert len(line) < 300
        assert line.endswith("...")

    def test_the_layer_name_is_quoted(self) -> None:
        """Names contain spaces; an unquoted one makes the line ambiguous."""
        assert "'Roads and trails'" in describe_layer(
            "Roads and trails", vector_tile_uri(_PORTAL, _ITEM)
        )
