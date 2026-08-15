# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rewrite portal COG layers in a saved project so it will open again.

A project saved by an older plugin can contain a raster layer read
through GDAL's ``/vsicurl``. Opening it deadlocks QGIS: providers are
built on a worker pool during project read and the GUI thread blocks
until they finish, and a ``/vsicurl`` provider never finishes. The
plugin stopped producing those layers, but it cannot repair a project
it can never load, so this does it from outside QGIS.

Each affected layer is repointed at the portal's XYZ tile route, which
is the same imagery through QGIS's own network stack. The original file
is kept alongside as ``<name>.qgz.bak``.

    python scripts/repair_project.py path/to/project.qgz
    python scripts/repair_project.py path/to/project.qgz --dry-run

A .qgs (uncompressed) project is handled too.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

#: ``/vsicurl/<portal>/api/tile-layer/<id>/file.cog``, as the old
#: builder wrote it. Captured out of an XML attribute, so it stops at
#: the quote.
_COG = re.compile(
    r"/vsicurl/(?P<portal>https?://[^\"'<>\s]+?)/api/tile-layer/"
    r"(?P<item>[0-9a-fA-F-]+)/file\.cog"
)


def xyz_uri(portal: str, item_id: str) -> str:
    """The tile route for the same item.

    Deliberately a copy of ``browser.uris.tile_layer_xyz_uri`` rather
    than an import: this script has to run against a broken project
    without the plugin importable, which is the state anyone reaching
    for it is in.
    """
    template = f"{portal}/api/tile-layer/{item_id}/tiles/{{z}}/{{x}}/{{y}}.png"
    return f"type=xyz&url={quote(template, safe='')}&zmin=0&zmax=18"


def repair_xml(xml: str) -> tuple[str, list[str]]:
    """Return the rewritten XML and the item ids that were changed.

    The replacement is XML-escaped on the way in. A ``/vsicurl`` path
    contains no ``&``, an XYZ URI is nothing but ``&``-separated
    parameters, and dropping those in raw produces a project file QGIS
    refuses to parse: "Expected ';', but got '='". The first version of
    this did exactly that, and the repaired project opened instantly
    with zero layers, which reads like success unless you count them.
    """
    changed: list[str] = []

    def swap(match: re.Match[str]) -> str:
        changed.append(match.group("item"))
        return xyz_uri(
            match.group("portal"), match.group("item")
        ).replace("&", "&amp;")

    return _COG.sub(swap, xml), changed


def repair_file(path: Path, *, dry_run: bool) -> list[str]:
    if path.suffix.lower() == ".qgs":
        xml = path.read_text(encoding="utf-8")
        fixed, changed = repair_xml(xml)
        if changed and not dry_run:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            path.write_text(fixed, encoding="utf-8")
        return changed

    # .qgz is a zip holding one .qgs plus sidecars. Rewrite in place by
    # rebuilding the archive, keeping every other member byte for byte.
    with zipfile.ZipFile(path) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}

    changed: list[str] = []
    for name, blob in members.items():
        if not name.lower().endswith(".qgs"):
            continue
        fixed, found = repair_xml(blob.decode("utf-8"))
        if found:
            members[name] = fixed.encode("utf-8")
            changed.extend(found)

    if changed and not dry_run:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, blob in members.items():
                zf.writestr(name, blob)
    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, nargs="+")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and write nothing",
    )
    args = parser.parse_args(argv)

    total = 0
    for path in args.project:
        if not path.exists():
            print(f"{path}: not found")
            continue
        changed = repair_file(path, dry_run=args.dry_run)
        total += len(changed)
        if not changed:
            print(f"{path}: nothing to repair")
        elif args.dry_run:
            print(f"{path}: would repair {len(changed)} layer(s): "
                  f"{', '.join(changed)}")
        else:
            print(f"{path}: repaired {len(changed)} layer(s): "
                  f"{', '.join(changed)}")
            print(f"    original kept at {path.name}.bak")
    if total and not args.dry_run:
        print("\nOpen the project again; it should load.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
