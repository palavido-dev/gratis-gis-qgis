# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repairing a saved project that QGIS can no longer open.

A project holding a ``/vsicurl`` COG layer deadlocks QGIS on read, so
the plugin cannot repair it from inside: by the time plugin code could
run, the application is already hung. This runs from outside, which
means it must be right about the file format without QGIS to check it.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "scripts")
)

from repair_project import repair_file, repair_xml, xyz_uri

_PORTAL = "https://gratisgis.org"
_ITEM = "15be62b2-7af0-48f2-9f61-dfe7814e5050"
_COG = f"/vsicurl/{_PORTAL}/api/tile-layer/{_ITEM}/file.cog"


def _project_xml(*sources: str) -> str:
    layers = "\n".join(
        f'    <maplayer><datasource>{s}</datasource></maplayer>'
        for s in sources
    )
    return f'<?xml version="1.0"?>\n<qgis>\n{layers}\n</qgis>\n'


class TestRepairXml:
    def test_a_cog_source_becomes_the_tile_route(self) -> None:
        fixed, changed = repair_xml(_project_xml(_COG))
        assert changed == [_ITEM]
        assert "/vsicurl/" not in fixed
        assert "type=xyz&amp;url=" in fixed
        assert _ITEM in fixed

    def test_the_replacement_is_xml_escaped(self) -> None:
        """A /vsicurl path has no ``&``; an XYZ URI is full of them.

        Dropping them in raw makes a project QGIS will not parse
        ("Expected ';', but got '='"), and the damage is quiet: the
        repaired project opens instantly with zero layers, which looks
        like success unless you count them. The first version of this
        script did exactly that and the end-to-end check caught it.
        """
        import xml.etree.ElementTree as ET

        fixed, _ = repair_xml(_project_xml(_COG))
        root = ET.fromstring(fixed)  # raises if the escaping is wrong
        [source] = [el.text or "" for el in root.iter("datasource")]
        # Parsed back, the text is the real URI with bare ampersands.
        assert source.startswith("type=xyz&url=")
        assert "&amp;" not in source

    def test_the_repaired_project_still_parses_as_xml(self) -> None:
        import xml.etree.ElementTree as ET

        fixed, _ = repair_xml(
            _project_xml("C:/data/x.gpkg", _COG, "type=xyz&amp;url=x")
        )
        assert len(list(ET.fromstring(fixed).iter("datasource"))) == 3

    def test_the_portal_is_taken_from_the_source_not_assumed(self) -> None:
        """Someone's project may point at a different portal.

        Hardcoding one would silently repoint their layers at ours.
        """
        other = f"/vsicurl/https://gis.example.org/api/tile-layer/{_ITEM}/file.cog"
        fixed, changed = repair_xml(_project_xml(other))
        assert changed == [_ITEM]
        assert "gis.example.org" in fixed
        assert "gratisgis.org" not in fixed

    def test_other_layers_are_left_exactly_alone(self) -> None:
        """A project is mostly layers this script has no business in."""
        gpkg = "C:/data/clone.gpkg|layername=parcels"
        usgs = "type=xyz&url=https%3A%2F%2Fbasemap.nationalmap.gov%2Fx"
        fixed, changed = repair_xml(_project_xml(gpkg, _COG, usgs))
        assert len(changed) == 1
        assert gpkg in fixed
        assert usgs in fixed

    def test_several_cog_layers_are_all_repaired(self) -> None:
        second = f"/vsicurl/{_PORTAL}/api/tile-layer/aaaa-bbbb/file.cog"
        _fixed, changed = repair_xml(_project_xml(_COG, second))
        assert changed == [_ITEM, "aaaa-bbbb"]

    def test_a_project_with_nothing_to_fix_is_reported_as_such(self) -> None:
        fixed, changed = repair_xml(_project_xml("C:/data/x.gpkg"))
        assert changed == []
        assert fixed == _project_xml("C:/data/x.gpkg")

    def test_the_rewritten_uri_matches_what_the_plugin_now_builds(self) -> None:
        """The script cannot import the plugin, so the two can drift.

        Asserted against the real builder here, where the plugin IS
        importable, so a change to one that is not made to the other
        fails the build rather than a user's project.
        """
        from gratisgis_qgis.browser.uris import tile_layer_xyz_uri

        assert xyz_uri(_PORTAL, _ITEM) == tile_layer_xyz_uri(_PORTAL, _ITEM)


class TestRepairFile:
    def _qgz(self, tmp_path: Path, xml: str) -> Path:
        path = tmp_path / "p.qgz"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("p.qgs", xml)
            zf.writestr("p.qgd", b"sidecar bytes")
        return path

    def test_a_qgz_is_repaired_in_place(self, tmp_path: Path) -> None:
        path = self._qgz(tmp_path, _project_xml(_COG))
        assert repair_file(path, dry_run=False) == [_ITEM]
        with zipfile.ZipFile(path) as zf:
            assert "/vsicurl/" not in zf.read("p.qgs").decode()

    def test_the_other_archive_members_survive(self, tmp_path: Path) -> None:
        """A .qgz carries sidecars; rebuilding must not drop them.

        Losing the .qgd loses the project's embedded auxiliary storage,
        which is where things like edit widgets and joins live.
        """
        path = self._qgz(tmp_path, _project_xml(_COG))
        repair_file(path, dry_run=False)
        with zipfile.ZipFile(path) as zf:
            assert sorted(zf.namelist()) == ["p.qgd", "p.qgs"]
            assert zf.read("p.qgd") == b"sidecar bytes"

    def test_the_original_is_kept(self, tmp_path: Path) -> None:
        """This rewrites someone's project. Keep what was there."""
        path = self._qgz(tmp_path, _project_xml(_COG))
        repair_file(path, dry_run=False)
        backup = path.with_suffix(".qgz.bak")
        assert backup.exists()
        with zipfile.ZipFile(backup) as zf:
            assert "/vsicurl/" in zf.read("p.qgs").decode()

    def test_a_dry_run_changes_nothing_on_disk(self, tmp_path: Path) -> None:
        path = self._qgz(tmp_path, _project_xml(_COG))
        before = path.read_bytes()
        assert repair_file(path, dry_run=True) == [_ITEM]
        assert path.read_bytes() == before
        assert not path.with_suffix(".qgz.bak").exists()

    def test_a_clean_project_is_not_rewritten_or_backed_up(
        self, tmp_path: Path
    ) -> None:
        """No changes means no .bak clutter and no touched mtime."""
        path = self._qgz(tmp_path, _project_xml("C:/data/x.gpkg"))
        before = path.read_bytes()
        assert repair_file(path, dry_run=False) == []
        assert path.read_bytes() == before
        assert not path.with_suffix(".qgz.bak").exists()

    def test_an_uncompressed_qgs_is_handled_too(self, tmp_path: Path) -> None:
        path = tmp_path / "p.qgs"
        path.write_text(_project_xml(_COG), encoding="utf-8")
        assert repair_file(path, dry_run=False) == [_ITEM]
        assert "/vsicurl/" not in path.read_text(encoding="utf-8")
        assert (tmp_path / "p.qgs.bak").exists()
