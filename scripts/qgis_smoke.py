# SPDX-License-Identifier: AGPL-3.0-or-later
"""Headless smoke test against a REAL QGIS install.

The pytest suite runs against a fabricating ``qgis`` stub, which is
fast and hermetic but structurally blind to one class of bug: passing
a value of the wrong TYPE to a real Qt/QGIS API. A stub accepts
anything. PyQt6 does not. That is how a bare ``int`` reached
``QgsTask(description, flags)`` and crashed on the first sign-in even
though every unit test was green.

So this script exercises the plugin's Qt-facing seams against the real
bindings: every enum the plugin resolves, both QgsTask flag paths, a
task actually run through the real task manager, the data item
provider, and the layer URIs handed to real providers.

Run it with QGIS's own interpreter, not the repo venv:

    C:\\OSGeo4W\\bin\\python-qgis.bat scripts\\qgis_smoke.py

Exit code 0 means every check passed. It needs no portal and no
network: nothing here signs in or fetches data.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import traceback
from pathlib import Path

# Offscreen so this runs over SSH / in a headless shell without
# trying to open a window.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_failures: list[str] = []
_checks = 0


def check(label: str, fn) -> object:
    """Run one check, record the failure, keep going.

    Collecting failures instead of stopping at the first means one run
    reports every broken seam, which matters when the whole point is
    finding a class of bug rather than a single instance.
    """
    global _checks
    _checks += 1
    try:
        value = fn()
    except BaseException as exc:  # a smoke test wants every failure, not the first
        _failures.append(f"{label}: {type(exc).__name__}: {exc}")
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
        return None
    print(f"  ok    {label}")
    return value


def main() -> int:
    from qgis.core import QgsApplication

    print("QGIS smoke test (real bindings)")
    print(f"  python {sys.version.split()[0]}")

    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        _run_checks()
    finally:
        qgs.exitQgis()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} of {_checks} checks")
        for line in _failures:
            print(f"  - {line}")
        return 1
    print(f"PASSED: {_checks} checks")
    return 0


def _run_checks() -> None:
    from qgis.core import QgsTask, QgsVectorFileWriter

    print("\n[1] plugin modules import against real bindings")
    import importlib
    import pkgutil

    import gratisgis_qgis

    for mod in pkgutil.walk_packages(
        gratisgis_qgis.__path__, prefix="gratisgis_qgis."
    ):
        check(f"import {mod.name}", lambda n=mod.name: importlib.import_module(n))

    print("\n[2] enum resolution (the PyQt6 strict-enum class of bug)")
    # Every one of these resolves at import time, so reaching this
    # point already proves they resolved. Read them back anyway: the
    # assertion that matters is that a real binding accepts them, and
    # naming each one makes a future breakage report the exact enum.
    from gratisgis_qgis.browser import items as browser_items
    from gratisgis_qgis.browser import provider as browser_provider
    from gratisgis_qgis.qgis_compat import resolve_enum

    for name in (
        "_BROWSER_TYPE_NO_TYPE",
        "_BROWSER_CAP_FERTILE",
        "_BROWSER_CAP_FAST",
        "_POPULATED_STATE",
        "_LAYER_TYPE_VECTOR",
        "_LAYER_TYPE_RASTER",
        "_LAYER_TYPE_VECTOR_TILE",
    ):
        check(f"browser.items.{name}", lambda n=name: getattr(browser_items, n))
    check(
        "browser.provider._NET_CAPABILITY",
        lambda: browser_provider._NET_CAPABILITY,
    )
    check(
        "QgsVectorFileWriter NoError resolves",
        lambda: resolve_enum(
            (getattr(QgsVectorFileWriter, "WriterError", None), "NoError"),
            (QgsVectorFileWriter, "NoError"),
        ),
    )

    print("\n[3] QgsTask flags (the bug that reached a user)")
    from gratisgis_qgis.tasks import _cancel_flags

    cancelable = check("_cancel_flags(cancelable=True)", lambda: _cancel_flags(QgsTask, True))
    not_cancelable = check(
        "_cancel_flags(cancelable=False)", lambda: _cancel_flags(QgsTask, False)
    )
    # The regression that shipped: a bare int is rejected by PyQt6.
    check(
        "cancelable flags is not a bare int",
        lambda: _assert(
            type(cancelable) is not int,
            f"cancelable flags must not be a plain int, got {cancelable!r}",
        ),
    )
    check(
        "non-cancelable flags is not a bare int",
        lambda: _assert(
            type(not_cancelable) is not int,
            f"non-cancelable flags must not be a plain int, got {not_cancelable!r}",
        ),
    )

    print("\n[4] real QgsTask construction and execution")
    from gratisgis_qgis import tasks as tasks_mod

    for flag_label, flag_value in (("cancelable", True), ("non-cancelable", False)):
        check(
            f"construct _FnTask ({flag_label})",
            lambda v=flag_value: tasks_mod._build_fn_task_cls()(
                "smoke", lambda handle: None, lambda r: None, lambda e: None, v
            ),
        )

    check("run a task end to end through the real task manager", _run_one_task)

    print("\n[5] data item provider against the real browser API")
    from gratisgis_qgis.browser.provider import GratisGISDataItemProvider

    provider = check("construct provider", GratisGISDataItemProvider)
    if provider is not None:
        check("provider.name()", provider.name)
        check("provider.capabilities()", provider.capabilities)
        check("provider.createDataItem(root)", lambda: provider.createDataItem("", None))

    print("\n[6] layer URIs are accepted by the real providers")
    from qgis.core import QgsProviderRegistry

    from gratisgis_qgis.browser import uris

    registry = QgsProviderRegistry.instance()
    check(
        "provider 'vectortile' is available",
        lambda: _assert(
            "vectortile" in registry.providerList(),
            "vectortile provider missing from this QGIS build",
        ),
    )
    check(
        "provider 'OAPIF' or 'oapif' is available",
        lambda: _assert(
            any(p.lower() == "oapif" for p in registry.providerList()),
            "OAPIF provider missing from this QGIS build",
        ),
    )
    check(
        "public vector tile uri builds",
        lambda: uris.vector_tile_uri("https://example.test", "item-1"),
    )
    check(
        "authed vector tile uri builds",
        lambda: uris.authed_vector_tile_uri(
            "https://example.test", "item-1", "layer-1", authcfg_id="abc123"
        ),
    )
    check("oapif uri builds", lambda: uris.oapif_uri("https://example.test", "item-1"))
    check(
        "authed oapif uri builds",
        lambda: uris.authed_oapif_uri(
            "https://example.test", "item-1__roads", authcfg_id="abc123"
        ),
    )

    # A URI the provider cannot decode yields an empty layer with no
    # error dialog, which is the failure mode this whole authed-tile
    # path exists to remove. Decoding is offline: no tile is fetched.

    def _oapif_keeps_authcfg() -> None:
        uri = uris.authed_oapif_uri(
            "https://example.test", "i", authcfg_id="abc123"
        )
        _assert("authcfg='abc123'" in uri, "authcfg missing from the uri")
        # Where this build exposes decodeUri for the provider, ask it
        # too: a decoder that drops the authcfg would silently turn
        # private feature layers into anonymous requests.
        for key in ("OAPIF", "oapif"):
            meta = registry.providerMetadata(key)
            if meta is None:
                continue
            decoded = meta.decodeUri(uri)
            if decoded:
                _assert(
                    "abc123" in str(decoded),
                    f"decodeUri dropped the authcfg: {decoded!r}",
                )
            break

    check(
        "OAPIF provider keeps the authcfg on the signed-in uri",
        _oapif_keeps_authcfg,
    )
    check(
        "vectortile provider decodes the public uri",
        lambda: _assert_decodes(
            registry, "vectortile", uris.vector_tile_uri("https://example.test", "item-1")
        ),
    )
    check(
        "vectortile provider decodes the authed uri (and keeps authcfg)",
        lambda: _assert_decodes(
            registry,
            "vectortile",
            uris.authed_vector_tile_uri(
                "https://example.test", "item-1", "layer-1", authcfg_id="abc123"
            ),
            expect={"authcfg": "abc123"},
        ),
    )

    print("\n[7] dialog layer pickers accept the classes QGIS really builds")
    # Cloning shipped broken twice because the unit tests use one
    # stand-in class for every QGIS layer class, which makes an
    # isinstance check pass no matter which class the code asks for.
    # These checks use the real bindings, where the class hierarchy is
    # the actual fact in question.
    from types import SimpleNamespace

    from qgis.core import QgsProject, QgsVectorLayer, QgsVectorTileLayer

    from gratisgis_qgis.ui.clone_dialog import CloneToGeoPackageDialog

    check(
        "QgsVectorTileLayer is NOT a QgsVectorLayer (the premise of the bug)",
        lambda: _assert(
            not issubclass(QgsVectorTileLayer, QgsVectorLayer),
            "QgsVectorTileLayer now subclasses QgsVectorLayer; the clone "
            "picker's two-class check can be simplified",
        ),
    )

    tile_layer = QgsVectorTileLayer(
        uris.vector_tile_uri("https://example.test", "item-1__trails"), "Trails"
    )
    QgsProject.instance().addMapLayer(tile_layer)
    try:
        combo = _CollectingCombo()
        CloneToGeoPackageDialog._populate_layer_combo(
            SimpleNamespace(_layer_combo=combo)
        )
        check(
            "clone picker offers a real vector-tile layer",
            lambda: _assert(
                "Trails" in [text for text, _ in combo.items],
                f"clone picker did not offer the layer; it listed {combo.items!r}",
            ),
        )
    finally:
        QgsProject.instance().removeMapLayer(tile_layer.id())

    print("\n[8] overwriting a clone that is open in the project")
    # Windows refuses to replace a file another handle holds open,
    # which POSIX allows, so the safe-write promote failed on exactly
    # the case the overwrite prompt exists for. Whether removing the
    # layer releases the handle is a question only real QGIS answers.
    import os
    import shutil
    import tempfile

    from qgis.core import QgsCoordinateTransformContext

    from gratisgis_qgis.offline.clone import safe_write_path, source_targets_file
    from gratisgis_qgis.ui.clone_dialog import _project_layers_using

    work = tempfile.mkdtemp()
    target = os.path.join(work, "clone.gpkg")

    def write_gpkg(path):
        mem = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=name:string", "src", "memory"
        )
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = "clone"
        QgsVectorFileWriter.writeAsVectorFormatV3(
            mem, path, QgsCoordinateTransformContext(), options
        )

    try:
        write_gpkg(target)
        opened = QgsVectorLayer(f"{target}|layername=clone", "clone", "ogr")
        QgsProject.instance().addMapLayer(opened)

        check(
            "the open layer is found by path",
            lambda: _assert(
                [lyr.name() for lyr in _project_layers_using(target)] == ["clone"],
                "the layer holding the file open was not found",
            ),
        )
        check(
            "a real QGIS source string matches its file",
            lambda: _assert(
                source_targets_file(opened.source(), target),
                f"source did not match: {opened.source()!r}",
            ),
        )

        # READ the features. This line is the whole test.
        #
        # An earlier version removed the layer and asserted the
        # overwrite then succeeded, which it did, and the assertion was
        # worthless: a layer that has only been OPENED releases its
        # file, while one whose features have been read does not,
        # because reading puts the dataset in GDAL's pool and removing
        # the layer does not empty the pool. Every layer drawn on the
        # canvas has read its features, so real overwrites hit the
        # locked case and this test never did. It passed for two
        # releases while the second clone of a layer failed for the
        # user.
        check(
            "reading the layer's features (what puts it in GDAL's pool)",
            lambda: list(opened.getFeatures()),
        )

        def overwrite_fails_while_open():
            try:
                with safe_write_path(target) as tmp:
                    write_gpkg(tmp)
            except OSError:
                return True
            return False

        held = check("overwrite while open is refused", overwrite_fails_while_open)
        check(
            "no staging directory is left behind by the refusal",
            lambda: _assert(
                os.listdir(work) == ["clone.gpkg"],
                f"leftovers in the destination folder: {os.listdir(work)}",
            ),
        )

        for stale in _project_layers_using(target):
            QgsProject.instance().removeMapLayer(stale.id())

        # Removing the layer is NOT enough, which is the finding this
        # section exists to record. GDAL keeps the dataset pooled and
        # the rename stays refused, so the clone's second run failed
        # for the user while this test passed.
        def still_refused_after_removal():
            try:
                with safe_write_path(target) as tmp:
                    write_gpkg(tmp)
            except OSError:
                return True
            return False

        check(
            "removing the layer does NOT release the file",
            lambda: _assert(
                still_refused_after_removal() is True,
                "the rename worked after removal, so this QGIS releases "
                "pooled datasets and the in-place fallback is untested here",
            ),
        )

        def overwrite_in_place():
            with safe_write_path(target, allow_in_place=True) as tmp:
                write_gpkg(tmp)
            return True

        check("overwrite succeeds with allow_in_place", overwrite_in_place)
        check(
            "and still leaves nothing behind",
            lambda: _assert(
                os.listdir(work) == ["clone.gpkg"],
                f"leftovers after success: {os.listdir(work)}",
            ),
        )
        check(
            "the overwritten file is a readable GeoPackage",
            lambda: _assert(
                QgsVectorLayer(
                    f"{target}|layername=clone", "check", "ogr"
                ).isValid(),
                "the promoted file does not open; the in-place write "
                "produced something GDAL will not read",
            ),
        )
        if held is not True:
            print(
                "    note: this QGIS did not hold the file open, so the "
                "refusal path was not exercised"
            )
    finally:
        QgsProject.instance().clear()
        shutil.rmtree(work, ignore_errors=True)

    print("\n[9] recorded extents reach the layer")
    # A tiled layer reports the whole world until something applies the
    # portal's extent. Everything about that is a fact about the real
    # bindings (that no URI parameter sets it, that setExtent sticks,
    # that an unknown parameter survives into layer.source()), so stubs
    # cannot testify here at all.
    import os
    import tempfile

    from qgis.core import QgsRasterLayer

    from gratisgis_qgis.layer_extent import ExtentApplier

    # Randolph County parcels, as the portal reports it.
    bbox = (-79.8817405459576, 38.8075562828525, -79.72808554075921, 38.91672787868328)
    world_edge = 20037508.0

    applier = ExtentApplier()
    applier.install()
    try:
        cases = (
            (
                "vector tile",
                QgsVectorTileLayer(
                    uris.vector_tile_uri(
                        "https://example.test", "item-1__parcels", extent=bbox
                    ),
                    "parcels",
                ),
            ),
            (
                "xyz raster",
                QgsRasterLayer(
                    uris.tile_layer_xyz_uri(
                        "https://example.test", "item-1", extent=bbox
                    ),
                    "hillshade",
                    "wms",
                ),
            ),
        )
        for label, layer in cases:
            check(
                f"{label}: source keeps the recorded extent",
                lambda lyr=layer: _assert(
                    uris.parse_extent_suffix(lyr.source()) == bbox,
                    f"extent lost from source: {lyr.source()!r}",
                ),
            )
            QgsProject.instance().addMapLayer(layer)
            check(
                f"{label}: extent is the data, not the world",
                lambda lyr=layer: _assert(
                    abs(lyr.extent().xMinimum()) < world_edge
                    and lyr.extent().width() < world_edge,
                    f"extent still global: {lyr.extent().toString()}",
                ),
            )

        # A saved project reloads layers through a different path, and
        # the extent resets to global on the way, so the applier has to
        # see them again or the fix only survives one session.
        project_path = os.path.join(tempfile.mkdtemp(), "smoke.qgz")
        QgsProject.instance().write(project_path)
        QgsProject.instance().clear()
        QgsProject.instance().read(project_path)
        restored = list(QgsProject.instance().mapLayers().values())
        check(
            "reloading a project restores both layers",
            lambda: _assert(
                len(restored) == 2, f"expected 2 layers, got {len(restored)}"
            ),
        )
        for layer in restored:
            check(
                f"reloaded {layer.name()}: extent is still the data",
                lambda lyr=layer: _assert(
                    lyr.extent().width() < world_edge,
                    f"extent global after reload: {lyr.extent().toString()}",
                ),
            )
    finally:
        applier.remove()
        QgsProject.instance().clear()

    print("\n[10] a clone's pending changes survive being saved")
    # The point of the whole baseline design. The previous flow read
    # QGIS's unsaved edit buffer, so saving made edits invisible to the
    # plugin and closing QGIS lost them. Everything here happens
    # through SAVED state and a reopened layer.
    from gratisgis_qgis.offline.clone import (
        has_baseline,
        read_baseline,
        write_baseline,
    )
    from gratisgis_qgis.offline.reader import (
        baseline_from_features,
        portal_edited_stamps,
        read_local_features,
    )
    from gratisgis_qgis.offline.sync_state import plan_local_changes
    from gratisgis_qgis.ui.clone_dialog import _write_geojson_to_geopackage

    clone_dir = tempfile.mkdtemp()
    clone_path = os.path.join(clone_dir, "trails.gpkg")
    portal_body = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": f"gid-{n}",
                "geometry": {"type": "Point", "coordinates": [-79.8 + n / 100, 38.8]},
                "properties": {"_global_id": f"gid-{n}", "name": f"trail {n}",
                               "_edited_at": "2026-08-01T00:00:00Z"},
            }
            for n in range(3)
        ],
    }

    def reopen():
        """A fresh layer object, as a new QGIS session would build."""
        return QgsVectorLayer(f"{clone_path}|layername=trails", "trails", "ogr")

    def pending():
        layer = reopen()
        return plan_local_changes(read_local_features(layer), read_baseline(clone_path))

    try:
        from gratisgis_qgis.browser.uris import PortalLayerRef

        _write_geojson_to_geopackage(
            portal_body,
            clone_path,
            source=PortalLayerRef(
                portal_url="https://example.test", item_id="i1", layer_id="trails"
            ),
            portal_stamps=portal_edited_stamps(portal_body),
        )
        check(
            "the clone records a baseline",
            lambda: _assert(has_baseline(clone_path), "no baseline table was written"),
        )
        check(
            "the baseline covers every cloned feature",
            lambda: _assert(
                len(read_baseline(clone_path)) == 3,
                f"expected 3 baseline rows, got {len(read_baseline(clone_path))}",
            ),
        )
        check(
            "a fresh clone owes the portal nothing",
            lambda: _assert(
                pending() == [], f"unexpected pending changes: {pending()!r}"
            ),
        )

        # Edit and SAVE, which is what the old design could not survive.
        editable = reopen()
        editable.startEditing()
        target = next(editable.getFeatures())
        editable.changeAttributeValue(
            target.id(), editable.fields().indexOf("name"), "renamed"
        )
        _assert(editable.commitChanges(), "could not save the edit")
        del editable

        after_save = check("pending changes after saving", pending)
        check(
            "a saved edit is seen, from a reopened layer",
            lambda: _assert(
                [(c.kind, c.portal_id) for c in after_save] == [("update", "gid-0")],
                f"expected one update, got {after_save!r}",
            ),
        )
        check(
            "the edited attribute is what gets sent",
            lambda: _assert(
                after_save[0].properties.get("name") == "renamed",
                f"wrong payload: {after_save[0].properties!r}",
            ),
        )
        check(
            "bookkeeping columns are not sent as user attributes",
            lambda: _assert(
                "_portal_id" not in after_save[0].properties
                and "fid" not in after_save[0].properties,
                f"internal columns leaked: {after_save[0].properties!r}",
            ),
        )

        # QGIS has no editor tracking: it does not touch _edited_at when
        # you change an attribute. So detection must not depend on it,
        # and this proves it does not. The stamp in the local file is
        # still the cloned value while the edit is detected anyway.
        # (Reading it the other way round would be worse than useless:
        # the portal restamps _edited_at on every server-side write, so
        # counting it as user data would report an edit on features
        # nobody had touched.)
        stale = reopen()
        # Compared as text: OGR types this column as DateTime, so it
        # returns a QDateTime rather than the string the portal sent.
        # That mismatch is itself a reason the hash excludes the column
        # instead of trying to compare it across representations.
        stamps_now = {
            f["_global_id"]: _as_text(f["_edited_at"]) for f in stale.getFeatures()
        }
        del stale
        check(
            "the local edit stamp is untouched by QGIS, as expected",
            lambda: _assert(
                stamps_now.get("gid-0", "").startswith("2026-08-01"),
                f"something wrote the tracking column: {stamps_now!r}",
            ),
        )
        check(
            "and the edit is detected regardless of that stamp",
            lambda: _assert(
                [(c.kind, c.portal_id) for c in pending()] == [("update", "gid-0")],
                "detection is leaning on editor tracking QGIS never updates",
            ),
        )

        # A delete, also saved, also read back from a reopened layer.
        editable = reopen()
        editable.startEditing()
        editable.deleteFeature(next(editable.getFeatures()).id())
        _assert(editable.commitChanges(), "could not save the delete")
        del editable
        kinds = sorted(c.kind for c in pending())
        check(
            "a saved delete is seen too",
            lambda: _assert(
                kinds == ["delete"], f"expected a lone delete, got {kinds}"
            ),
        )

        # And once the baseline moves on, the clone is settled again.
        settled = reopen()
        write_baseline(
            clone_path,
            baseline_from_features(read_local_features(settled), {}),
        )
        del settled
        check(
            "recording a new baseline clears the pending list",
            lambda: _assert(
                pending() == [], f"still pending after resync: {pending()!r}"
            ),
        )
    finally:
        QgsProject.instance().clear()
        shutil.rmtree(clone_dir, ignore_errors=True)

    print("\n[11] the publish picker offers what is on the canvas")
    # The complaint this answers: publishing a raster made you find the
    # file on disk, when the thing you wanted to publish was already
    # drawn on your map. Whether a real QgsRasterLayer's source can be
    # traced back to its file is a question about the real bindings.
    from qgis.core import QgsRasterFileWriter, QgsRasterPipe

    from gratisgis_qgis.publish.source import resolve_raster_source
    from gratisgis_qgis.ui.publish_vector_dialog import (
        PublishLayerDialog,
        _raster_choice,
    )

    raster_dir = tempfile.mkdtemp()
    tif_path = os.path.join(raster_dir, "aerial.tif")
    try:
        # A real GeoTIFF on disk, written the way QGIS writes one.
        seed = QgsRasterLayer(
            uris.tile_layer_xyz_uri("https://example.test", "seed"), "seed", "wms"
        )
        writer = QgsRasterFileWriter(tif_path)
        pipe = QgsRasterPipe()
        made = False
        if seed.isValid() and pipe.set(seed.dataProvider().clone()):
            writer.writeRaster(
                pipe, 8, 8, seed.extent(), seed.crs()
            )
            made = os.path.isfile(tif_path)
        if not made:
            # Writing through the network provider is not the point of
            # the check; a plain file is enough to trace a path back.
            with open(tif_path, "wb") as fh:
                fh.write(b"stand-in raster bytes")

        file_layer = QgsRasterLayer(tif_path, "aerial", "gdal")
        check(
            "a file raster's source traces back to its file",
            lambda: _assert(
                resolve_raster_source(
                    file_layer.source(),
                    file_layer.dataProvider().name() if file_layer.isValid() else "gdal",
                ).file_path
                == tif_path,
                f"did not resolve: {file_layer.source()!r}",
            ),
        )

        service_layer = QgsRasterLayer(
            uris.tile_layer_xyz_uri("https://example.test", "item-1"),
            "streamed",
            "wms",
        )
        streamed = _raster_choice(service_layer, "L-service")
        check(
            "a streamed raster is refused, with a reason",
            lambda: _assert(
                not streamed.is_publishable and bool(streamed.reason),
                f"expected a refusal, got {streamed!r}",
            ),
        )
        check(
            "and the reason says how to fix it",
            lambda: _assert(
                "export" in streamed.reason.lower(),
                f"unhelpful wording: {streamed.reason!r}",
            ),
        )

        QgsProject.instance().addMapLayer(file_layer)
        QgsProject.instance().addMapLayer(service_layer)
        combo = _CollectingCombo()
        state = SimpleNamespace(_layer_combo=combo, _choices=[], _preselect_layer_id=None)
        PublishLayerDialog._populate_layer_combo(state)
        labels = [text for text, _ in combo.items]
        check(
            "both rasters are listed rather than one silently missing",
            lambda: _assert(
                any(t.startswith("aerial") for t in labels)
                and any(t.startswith("streamed") for t in labels),
                f"picker offered {labels!r}",
            ),
        )
        check(
            "the unpublishable one is marked as such",
            lambda: _assert(
                any(
                    t.startswith("streamed") and "cannot be published" in t
                    for t in labels
                ),
                f"no marker on the streamed layer: {labels!r}",
            ),
        )
    finally:
        QgsProject.instance().clear()
        shutil.rmtree(raster_dir, ignore_errors=True)

    print("\n[12] every toolbar icon still resolves")
    # The toolbar uses the plugin's own branded SVGs, each backed by a
    # QGIS theme icon as its fallback. Both halves are checked: a
    # bundled file that is missing or unloadable ships as a stock
    # button (wrong look), and a retired theme name would make that
    # degradation a BLANK button. Only a real QGIS can answer either.
    import re as _re

    from qgis.core import QgsApplication
    from qgis.PyQt.QtGui import QIcon

    from gratisgis_qgis import plugin as plugin_mod

    plugin_dir = os.path.dirname(os.path.abspath(plugin_mod.__file__))
    with open(plugin_mod.__file__, encoding="utf-8") as fh:
        source = fh.read()
    brand_names = [
        n for n in _re.findall(r'"([a-z-]+\.svg)"', source)
        # icon.svg is the plugin logo, loaded by a different helper
        # and checked separately below.
        if n != "icon.svg"
    ]
    theme_names = _re.findall(r'"(/m[A-Za-z0-9]+\.svg|/search\.svg)"', source)
    check(
        "the toolbar names seven branded icons",
        lambda: _assert(
            len(brand_names) == 7,
            f"expected 7 brand icon names, found {brand_names!r}",
        ),
    )
    for name in brand_names:
        path = os.path.join(plugin_dir, "resources", "icons", name)
        check(
            f"brand icon {name} is bundled",
            lambda p=path, n=name: _assert(
                os.path.isfile(p), f"resources/icons/{n} is not in the package"
            ),
        )
        check(
            f"brand icon {name} loads",
            lambda p=path, n=name: _assert(
                not QIcon(p).pixmap(24, 24).isNull(),
                f"{n} exists but renders to a null pixmap",
            ),
        )
    check(
        "the brand icons are not all the same one",
        lambda: _assert(
            len(set(brand_names)) == len(brand_names),
            f"duplicate icons would make the buttons indistinguishable: {brand_names!r}",
        ),
    )
    for icon_name in theme_names:
        check(
            f"fallback theme icon {icon_name}",
            lambda n=icon_name: _assert(
                not QgsApplication.getThemeIcon(n).isNull(),
                f"QGIS has no theme icon named {n}; the fallback would be blank",
            ),
        )
    check(
        "the plugin logo is the portal mark and loads",
        lambda: _assert(
            not QIcon(
                os.path.join(plugin_dir, "resources", "icon.svg")
            ).pixmap(24, 24).isNull(),
            "resources/icon.svg renders to a null pixmap",
        ),
    )

    print("\n[12b] open a portal map against real bindings")
    _check_open_map()

    print("\n[12c] drop-to-publish still has its QGIS seams")
    # The drop path rides two APIs a stub cannot vouch for: the
    # virtual acceptDrop/handleDrop pair on data items, and the mime
    # decoder. If either leaves the API, the gesture dies silently.
    from qgis.core import QgsDataItem as _QgsDataItem
    from qgis.core import QgsMimeDataUtils as _MimeUtils

    for name in ("acceptDrop", "handleDrop"):
        check(
            f"QgsDataItem.{name} exists on this build",
            lambda n=name: _assert(
                callable(getattr(_QgsDataItem, n, None)),
                f"QgsDataItem.{n} is gone; drop-to-publish needs a "
                "QgsDataItemGuiProvider port",
            ),
        )
    check(
        "QgsMimeDataUtils.decodeUriList exists",
        lambda: _assert(
            callable(getattr(_MimeUtils, "decodeUriList", None)),
            "decodeUriList is gone",
        ),
    )

    print("\n[12d] Processing provider against the real registry")
    # The provider and algorithms inherit Processing base classes that
    # only exist here. Registration is the crash surface: a bad
    # parameter definition or a missing virtual aborts addProvider.
    from qgis.core import QgsApplication as _QgsApp2

    from gratisgis_qgis.processing import GratisGISProcessingProvider
    from gratisgis_qgis.processing.provider import (
        CloneLayerAlgorithm,
        PublishLayersAsItemAlgorithm,
        PublishVectorLayerAlgorithm,
    )

    check("construct PublishVectorLayerAlgorithm", PublishVectorLayerAlgorithm)
    check("construct PublishLayersAsItemAlgorithm", PublishLayersAsItemAlgorithm)
    check("construct CloneLayerAlgorithm", CloneLayerAlgorithm)
    provider2 = check("construct the provider", GratisGISProcessingProvider)
    if provider2 is not None:
        registry = _QgsApp2.processingRegistry()
        check(
            "register with the real Processing registry",
            lambda: _assert(
                registry.addProvider(provider2),
                "addProvider returned False",
            ),
        )
        try:
            algs = provider2.algorithms()
            check(
                "all three algorithms loaded",
                lambda: _assert(
                    sorted(a.name() for a in algs)
                    == ["clonelayer", "publishlayersasitem",
                        "publishvectorlayer"],
                    f"algorithms: {[a.name() for a in algs]!r}",
                ),
            )
            for alg in algs:
                check(
                    f"algorithm {alg.name()} declares its parameters",
                    lambda a=alg: _assert(
                        len(a.parameterDefinitions()) >= 3,
                        f"{a.name()} has {len(a.parameterDefinitions())} params",
                    ),
                )
        finally:
            registry.removeProvider(provider2.id())

    print("\n[13] auth: the API Header method private layers depend on")
    from gratisgis_qgis.auth_bridge import find_api_header_method

    method = check("find_api_header_method()", find_api_header_method)
    check(
        "this QGIS build ships an API Header auth method",
        lambda: _assert(
            method is not None,
            "no API Header auth method found; private layers would "
            "silently fall back to public-only rendering",
        ),
    )

    print("\n[14] freeze diagnostics (the project-load hang)")
    _check_freeze_diagnostics()

    print("\n[15] sign-out leaves a resolvable, credential-free authcfg")
    _check_signed_out_authcfg()

    print("\n[16] a replaced clone keeps its symbology and its place")
    _check_layer_placement()

    print("\n[17] splitting a feature in a clone sends both halves")
    _check_split_feature()

    print("\n[18] portal layers stay recognised after a project round trip")
    _check_uri_survives_reload()


def _check_uri_survives_reload() -> None:
    """Is a portal layer still ours after QGIS has rewritten its URI?

    A provider URI is an unordered parameter bag and QGIS spells it
    back its own way. Recognition used to key off the string starting
    with ``type=xyz``, so a reordered URI made a portal layer invisible
    to publish-as-map, the clone picker, the sync picker and the load
    trace. The layer still drew, which is why it read as "the publish
    dialog is broken" rather than as a parsing bug.

    Asked of a real save-and-reload rather than of a string written
    here, because the whole failure was a guess about how QGIS spells
    these and the guess matched our own builders.
    """
    import os
    import tempfile

    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorTileLayer

    from gratisgis_qgis.browser.uris import (
        authed_vector_tile_uri,
        parse_portal_layer_source,
        parse_tile_layer_uri,
        tile_layer_xyz_uri,
    )

    portal = "https://gratisgis.org"
    raster_item = "ed98bb41-053d-4317-897d-bf124d6a9dcd"
    vector_item = "71ec2071-1243-4621-a0cb-623edfebd467"
    layer_key = "lyr_1ch2vc9x"

    work = tempfile.mkdtemp(prefix="gg-uri-")
    project_path = os.path.join(work, "round-trip.qgz")
    try:
        raster = QgsRasterLayer(
            tile_layer_xyz_uri(portal, raster_item, authcfg_id="e53df68"),
            "hillshade",
            "wms",
        )
        vector = QgsVectorTileLayer(
            authed_vector_tile_uri(
                portal, vector_item, layer_key, authcfg_id="e53df68"
            ),
            "buildings",
        )
        project = QgsProject.instance()
        project.addMapLayer(raster)
        project.addMapLayer(vector)
        check("save the project", lambda: project.write(project_path))
        project.clear()
        check("reload it", lambda: project.read(project_path))

        reloaded = list(project.mapLayers().values())
        check(
            "both layers came back",
            lambda: _assert(
                len(reloaded) == 2, f"got {len(reloaded)} layers back"
            ),
        )

        by_name = {lyr.name(): lyr for lyr in reloaded}
        hillshade = by_name.get("hillshade")
        buildings = by_name.get("buildings")

        check(
            "the reloaded raster is still recognised as a portal tile layer",
            lambda: _assert(
                hillshade is not None
                and parse_tile_layer_uri(hillshade.source())
                == (portal, raster_item),
                "publish-as-map would offer this as an outside service: "
                f"{hillshade.source() if hillshade else None!r}",
            ),
        )
        check(
            "the reloaded vector tile layer is still recognised",
            lambda: _assert(
                buildings is not None
                and parse_portal_layer_source(buildings.source()) is not None,
                "the clone and sync pickers would stop offering this: "
                f"{buildings.source() if buildings else None!r}",
            ),
        )
        # Report what QGIS actually did, so a future reader does not
        # have to rediscover it from a user's screenshot.
        if hillshade is not None:
            print(f"    QGIS stores it as: {hillshade.source()[:100]}")
    finally:
        QgsProject.instance().clear()
        shutil.rmtree(work, ignore_errors=True)


def _check_split_feature() -> None:
    """Does QGIS's split really copy the portal id onto the new part?

    The whole fix rests on that being true, and it is exactly the sort
    of thing a stub would be written to agree with. So it is asked of
    the real splitFeatures: split a polygon, save, and count how many
    rows come back carrying the original's id.

    Reported by a user as "I split a polygon, synced, and the bottom
    half disappeared from the portal", plus "it still says one edit to
    sync". Both are the same collision.
    """
    import shutil
    import tempfile

    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsPointXY,
        QgsProject,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QVariant

    from gratisgis_qgis.offline.clone import PORTAL_ID_PROPERTY
    from gratisgis_qgis.offline.reader import (
        baseline_from_features,
        read_local_features,
    )
    from gratisgis_qgis.offline.sync_state import plan_local_changes

    portal_id = "01a00350-f0c5-71b6-9aea-1b5088dc676a"
    work = tempfile.mkdtemp(prefix="gg-split-")
    try:
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "clone", "memory")
        layer.dataProvider().addAttributes([
            QgsField(PORTAL_ID_PROPERTY, QVariant.String),
            QgsField("owner", QVariant.String),
        ])
        layer.updateFields()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(
            QgsGeometry.fromPolygonXY([[
                QgsPointXY(0, 0), QgsPointXY(10, 0),
                QgsPointXY(10, 10), QgsPointXY(0, 10), QgsPointXY(0, 0),
            ]])
        )
        feat.setAttributes([portal_id, "matt"])
        layer.dataProvider().addFeatures([feat])

        before = check(
            "the clone starts with one feature", lambda: layer.featureCount()
        )
        baseline = baseline_from_features(read_local_features(layer))
        check(
            "and one baseline entry",
            lambda: _assert(len(baseline) == 1, f"baseline: {baseline!r}"),
        )

        # Split it straight down the middle, the way the user did.
        check("start editing", layer.startEditing)
        check(
            "split the polygon in two",
            lambda: layer.splitFeatures(
                [QgsPointXY(5, -1), QgsPointXY(5, 11)], 0
            ),
        )
        check("save the split", layer.commitChanges)
        check(
            "the split produced a second feature",
            lambda: _assert(
                layer.featureCount() == 2,
                f"expected 2 features after the split, got "
                f"{layer.featureCount()} (was {before})",
            ),
        )

        live = check("read the saved rows", lambda: read_local_features(layer))
        sharing = [f for f in (live or []) if f.global_id == portal_id]
        check(
            "BOTH halves carry the original's portal id",
            lambda: _assert(
                len(sharing) == 2,
                "QGIS did not copy the portal id onto the new part, so "
                f"the premise of this fix is wrong: {[f.global_id for f in (live or [])]}",
            ),
        )

        changes = check(
            "plan the changes", lambda: plan_local_changes(live or [], baseline)
        )
        kinds = sorted(c.kind for c in (changes or []))
        check(
            "one update and one create, not a single update",
            lambda: _assert(
                kinds == ["create", "update"],
                f"the second half would be lost: {kinds}",
            ),
        )
        check(
            "the two changes carry different portal ids",
            lambda: _assert(
                len({c.portal_id for c in (changes or [])}) == 2,
                "both ops name the same feature, so the plan builder "
                "will merge them and one half disappears again",
            ),
        )
        check(
            "the new half keeps its own geometry",
            lambda: _assert(
                all(c.geometry is not None for c in (changes or [])),
                "a change went out with no geometry",
            ),
        )
    finally:
        QgsProject.instance().clear()
        shutil.rmtree(work, ignore_errors=True)


def _check_layer_placement() -> None:
    """Carrying styling and tree position across an overwrite (#17).

    Overwriting a clone removes the layer to release the Windows file
    lock and loads a fresh one, which discarded any symbology the user
    had applied. Whether a captured style survives the round trip is a
    question about real QGIS serialisation, and a stub asked the same
    question would only confirm the stub.
    """
    import shutil
    import tempfile

    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsFillSymbol,
        QgsGeometry,
        QgsLayerTreeLayer,
        QgsPointXY,
        QgsProject,
        QgsSingleSymbolRenderer,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QVariant

    from gratisgis_qgis.layer_placement import (
        capture_placement,
        restore_placement,
    )

    work = tempfile.mkdtemp(prefix="gg-placement-")
    try:
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Parcels", "memory")
        layer.dataProvider().addAttributes([QgsField("owner", QVariant.String)])
        layer.updateFields()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(
            QgsGeometry.fromPolygonXY([[
                QgsPointXY(0, 0), QgsPointXY(1, 0),
                QgsPointXY(1, 1), QgsPointXY(0, 0),
            ]])
        )
        layer.dataProvider().addFeatures([feat])

        # A symbology the user would notice losing.
        symbol = QgsFillSymbol.createSimple(
            {"color": "255,0,0,255", "outline_color": "0,0,255,255"}
        )
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setOpacity(0.42)

        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        root = project.layerTreeRoot()
        group = root.addGroup("Reference")
        group.addChildNode(QgsLayerTreeLayer(layer))
        # A sibling above it, so "restored to index 1" means something
        # more than "happens to be first".
        other = QgsVectorLayer("Point?crs=EPSG:4326", "Marks", "memory")
        project.addMapLayer(other, False)
        group.insertChildNode(0, QgsLayerTreeLayer(other))

        placement = check("capture_placement() on a styled layer", lambda: capture_placement(layer))
        check(
            "the style was captured",
            lambda: _assert(
                placement is not None and placement.style_xml,
                "no style XML captured; an overwrite would lose symbology",
            ),
        )
        check(
            "the group path was captured",
            lambda: _assert(
                placement is not None and placement.group_path == ["Reference"],
                f"unexpected group path: {getattr(placement, 'group_path', None)!r}",
            ),
        )
        check(
            "the position within the group was captured",
            lambda: _assert(
                placement is not None and placement.index == 1,
                f"unexpected index: {getattr(placement, 'index', None)!r}",
            ),
        )

        # Now the replacement: a brand new layer, as an overwrite builds.
        project.removeMapLayer(layer.id())
        replacement = QgsVectorLayer(
            "Polygon?crs=EPSG:4326", "Parcels (offline)", "memory"
        )
        project.addMapLayer(replacement)
        check(
            "the replacement starts with default styling",
            lambda: _assert(
                replacement.opacity() != 0.42,
                "the fresh layer already had the old opacity; the test proves nothing",
            ),
        )

        check(
            "restore_placement() on the replacement",
            lambda: restore_placement(replacement, placement),
        )
        check(
            "the symbology came back",
            lambda: _assert(
                abs(replacement.opacity() - 0.42) < 1e-6,
                f"opacity is {replacement.opacity()}, expected 0.42",
            ),
        )
        # Opacity alone would pass for a layer property that survived
        # without the renderer coming with it, so the fill colour is
        # checked too: that one can only have arrived through the
        # symbology.
        check(
            "the renderer's fill colour came back too",
            lambda: _assert(
                replacement.renderer() is not None
                and replacement.renderer().symbol().color().getRgb()[:3]
                == (255, 0, 0),
                "the restored renderer is not the captured one: "
                f"{replacement.renderer() and replacement.renderer().symbol().color().getRgb()}",
            ),
        )
        restored_node = root.findLayer(replacement.id())
        check(
            "it is back inside its group",
            lambda: _assert(
                restored_node is not None
                and restored_node.parent() is not None
                and str(restored_node.parent().name()) == "Reference",
                "the replacement did not return to its group",
            ),
        )
        check(
            "at the position it had",
            lambda: _assert(
                restored_node is not None
                and list(restored_node.parent().children()).index(restored_node) == 1,
                "the replacement did not return to its position",
            ),
        )
    finally:
        QgsProject.instance().clear()
        shutil.rmtree(work, ignore_errors=True)


def _check_open_map() -> None:
    """A whole portal map built into a real project, offline.

    The planner is unit-tested; what only a real QGIS can answer is
    whether the plan EXECUTES: vector tile and XYZ layers built from
    portal URIs, portal symbology turned into a vector tile renderer
    QGIS accepts, group nesting, visibility, and the stacking order.
    No tile is fetched; construction is lazy.
    """
    from qgis.core import QgsProject, QgsVectorTileLayer

    from gratisgis_qgis.open_map import open_map_in_project, plan_map_open
    from gratisgis_qgis.symbology import apply_portal_style

    portal = "https://gratisgis.org"
    item = {
        "id": "d1",
        "access": "private",
        "bbox": [-80.1, 38.7, -80.0, 38.8],
        "data": {
            "version": 3,
            "layers": [
                {"id": "parcels", "label": "Parcels", "geometryType": "Polygon"}
            ],
        },
    }
    map_data = {
        "version": 1,
        "basemap": "bm1",
        "center": [-80.06, 38.74],
        "zoom": 12,
        "layers": [
            {"id": "g", "title": "Overlays", "source": {"kind": "group"}},
            {
                "id": "a",
                "title": "Parcels",
                "visible": False,
                "opacity": 0.6,
                "groupId": "g",
                "source": {"kind": "data-layer", "itemId": "d1",
                           "layerKey": "parcels"},
                "style": {"polygon": {"fillColor": "#639922",
                                      "fillOpacity": 0.4,
                                      "strokeColor": "#27500a",
                                      "strokeWidth": 1.5}},
                "renderer": {"kind": "unique-values", "field": "class",
                             "categories": [
                                 {"value": "res", "color": "#7f77dd"}]},
            },
            {
                "id": "b",
                "title": "Feed",
                "source": {"kind": "geojson-url", "url": "https://x/f.json"},
            },
        ],
    }
    referenced = {
        "d1": item,
        "bm1": {"id": "bm1", "title": "Streets", "access": "public",
                "data": {"tileUrl": "https://tile.example/{z}/{x}/{y}.png"}},
    }
    plan = check(
        "plan a realistic map",
        lambda: plan_map_open(
            "Smoke map", map_data, referenced,
            portal_url=portal, layer_authcfg_id="e53df68",
        ),
    )
    if plan is None:
        return
    check(
        "the plan has one layer, one skip, and a basemap",
        lambda: _assert(
            len(plan.layers) == 1 and len(plan.skipped) == 1
            and plan.basemap is not None,
            f"unexpected plan shape: {plan!r}",
        ),
    )

    project = QgsProject.instance()
    project.clear()
    try:
        result = check(
            "execute the plan into a real project",
            lambda: open_map_in_project(plan, None),
        )
        if result is None:
            return
        added, problems = result
        check(
            "both buildable layers were added",
            lambda: _assert(added == 2, f"added {added}, problems {problems!r}"),
        )
        check(
            "the skip reason survived to the report",
            lambda: _assert(
                any("Feed" in p for p in problems),
                f"no line about the skipped layer: {problems!r}",
            ),
        )
        root = project.layerTreeRoot()
        group = root.findGroup("Smoke map")
        check(
            "the map became a group named after itself",
            lambda: _assert(group is not None, "no group node"),
        )
        subgroup = group.findGroup("Overlays") if group else None
        check(
            "the portal group became a nested QGIS group",
            lambda: _assert(subgroup is not None, "no Overlays subgroup"),
        )
        if subgroup is not None:
            nodes = subgroup.findLayers()
            check(
                "the layer sits inside its group, visibility applied",
                lambda: _assert(
                    len(nodes) == 1
                    and nodes[0].layer().name() == "Parcels"
                    and not nodes[0].isVisible(),
                    "layer missing from group or still visible",
                ),
            )
            layer = nodes[0].layer()
            check(
                "the portal layer is a real vector tile layer",
                lambda: _assert(
                    isinstance(layer, QgsVectorTileLayer),
                    f"unexpected type {type(layer).__name__}",
                ),
            )
            check(
                "portal symbology produced a categorised tile renderer",
                lambda: _assert(
                    len(layer.renderer().styles()) >= 6,
                    "expected category styles plus base styles",
                ),
            )
            check(
                "opacity carried through",
                lambda: _assert(
                    abs(layer.opacity() - 0.6) < 0.001,
                    f"opacity {layer.opacity()}",
                ),
            )
        check(
            "re-applying styles to a raster reports False, not a crash",
            lambda: _assert(
                apply_portal_style(object(), None, None) is False,
                "expected a calm refusal",
            ),
        )

        # The feature-default path: small layers now open as TRUE
        # vector layers, and the same portal style must land on them
        # as a rule-based renderer (one rule per category plus the
        # base catch-all), not silently no-op.
        from qgis.core import QgsRuleBasedRenderer, QgsVectorLayer

        memory = QgsVectorLayer("Polygon?crs=EPSG:4326", "Parcels", "memory")
        styled = check(
            "portal styling applies to a true vector layer",
            lambda: _assert(
                apply_portal_style(
                    memory,
                    {"polygon": {"fillColor": "#639922",
                                 "strokeColor": "#27500a"}},
                    {"kind": "unique-values", "field": "class",
                     "categories": [{"value": "res", "color": "#7f77dd"}]},
                )
                is True,
                "apply_portal_style refused a QgsVectorLayer",
            ),
        )
        if styled is not None:
            check(
                "the vector renderer is rule-based: category + base",
                lambda: _assert(
                    isinstance(memory.renderer(), QgsRuleBasedRenderer)
                    and len(memory.renderer().rootRule().children()) == 2,
                    f"renderer {type(memory.renderer()).__name__}",
                ),
            )
    finally:
        project.clear()


def _check_signed_out_authcfg() -> None:
    """The emptied-not-deleted authcfg, against the real auth manager.

    Sign-out used to delete the entry, stranding every saved project and
    every canvas layer on an id that no longer resolved: QGIS reports
    "FAILED to load config <id> from any storage". The replacement keeps
    the entry and strips the credential, which only works if a real
    QgsAuthManager accepts a config holding nothing but a marker header
    and still loads it back. A stub cannot answer that, and the failure
    mode if it is wrong is the exact banner the change exists to remove.
    """
    from gratisgis_qgis.auth_bridge import (
        SIGNED_OUT_HEADER,
        clear_api_header_credential,
        find_api_header_method,
        remove_authcfg,
        store_api_header_authcfg,
    )

    def read_api_header(cfg_id: str) -> tuple[str, str] | None:
        """Read back one stored API Header entry as (name, value).

        Local to the smoke test on purpose: production code stopped
        reading headers back when the GDAL path was removed, so the
        plugin-side helper was deleted as dead code, but THIS check
        still has to observe what a real auth manager stored.
        """
        from qgis.core import QgsApplication, QgsAuthMethodConfig

        cfg = QgsAuthMethodConfig()
        ok = QgsApplication.authManager().loadAuthenticationConfig(
            cfg_id, cfg, True
        )
        if not ok or not cfg.isValid():
            return None
        for header in cfg.configMap():
            value = cfg.config(header)
            if value:
                return (header, value)
        return None

    method = find_api_header_method()
    if method is None:
        check(
            "API Header method available for the sign-out check",
            lambda: _assert(False, "no API Header auth method; cannot verify"),
        )
        return

    authcfg_id = "ggsmoke1"
    try:
        stored = check(
            "store a credential-bearing authcfg",
            lambda: store_api_header_authcfg(
                authcfg_id,
                name="GratisGIS smoke",
                method_key=method,
                headers={"Authorization": "Bearer ggk_smoke"},
            ),
        )
        check("the credential stored", lambda: _assert(bool(stored), "store failed"))
        check(
            "the credential reads back",
            lambda: _assert(
                read_api_header(authcfg_id) == ("Authorization", "Bearer ggk_smoke"),
                f"unexpected read-back: {read_api_header(authcfg_id)!r}",
            ),
        )

        emptied = check(
            "clear_api_header_credential() on a real auth manager",
            lambda: clear_api_header_credential(
                authcfg_id, name="GratisGIS smoke"
            ),
        )
        check(
            "emptying reports success",
            lambda: _assert(bool(emptied), "could not empty the entry"),
        )

        # The two properties the whole design rests on.
        after = read_api_header(authcfg_id)
        check(
            "the entry still resolves after being emptied",
            lambda: _assert(
                after is not None,
                "the emptied entry no longer loads, which is the dangling "
                "reference this change exists to prevent",
            ),
        )
        check(
            "and carries no credential",
            lambda: _assert(
                after == SIGNED_OUT_HEADER,
                f"expected only the signed-out marker, got {after!r}",
            ),
        )
        check(
            "no Authorization header survives",
            lambda: _assert(
                (after or ("", ""))[0].lower() != "authorization",
                f"a credential header survived sign-out: {after!r}",
            ),
        )

        # Writing the database is not the same as changing what goes on
        # the wire. The auth method caches the resolved header per
        # authcfg id, so without a cache clear every layer kept sending
        # the old key until QGIS restarted. That this build even offers
        # the call is worth asserting: without it there is no way to
        # make a sign-out take effect in the running session.
        from qgis.core import QgsApplication as _QgsApp

        from gratisgis_qgis.auth_bridge import forget_cached_authcfg

        check(
            "this QGIS can be told to forget a cached auth config",
            lambda: _assert(
                hasattr(_QgsApp.authManager(), "clearCachedConfig"),
                "no clearCachedConfig(); a signed-out session would keep "
                "sending the old key until QGIS is restarted",
            ),
        )
        check(
            "forget_cached_authcfg() against the real auth manager",
            lambda: _assert(
                forget_cached_authcfg(authcfg_id) is True,
                "the cache clear did not report success",
            ),
        )
        check(
            "and the entry still reads back afterwards",
            lambda: _assert(
                read_api_header(authcfg_id) == SIGNED_OUT_HEADER,
                "clearing the cache damaged the stored entry: "
                f"{read_api_header(authcfg_id)!r}",
            ),
        )
    finally:
        with contextlib.suppress(Exception):
            remove_authcfg(authcfg_id)


def _check_freeze_diagnostics() -> None:
    """The freeze instrumentation, against the bindings it depends on.

    Everything here is a question only a real QGIS can answer, and each
    one is load bearing: if the heartbeat does not tick, or the signal
    does not exist, the diagnostic reports nothing and does so silently,
    which is the exact failure mode it was built to end.
    """
    import time

    from qgis.core import QgsApplication, QgsProject
    from qgis.PyQt.QtCore import QCoreApplication, QTimer

    from gratisgis_qgis.freeze_watch import (
        FreezeWatchdog,
        thread_roster,
        write_dump,
    )
    from gratisgis_qgis.load_trace import LoadTracer, auth_db_state, describe_layer
    from gratisgis_qgis.log import log_directory

    # The state read at project-load time. It must not prompt, must not
    # raise, and must not need the auth database unlocked: asking a
    # question that could raise the very modal prompt under
    # investigation would be its own bug.
    state = check("auth_db_state() answers without prompting", auth_db_state)
    check(
        "auth_db_state() is one of the known answers",
        lambda: _assert(
            state in ("unlocked", "locked (QGIS will prompt if a layer needs it)"),
            f"unexpected auth database state {state!r}",
        ),
    )
    check(
        "masterPasswordIsSet exists on this build",
        lambda: _assert(
            hasattr(QgsApplication.authManager(), "masterPasswordIsSet"),
            "no masterPasswordIsSet(); the load trace cannot report the "
            "one piece of state the freeze theory turns on",
        ),
    )

    # The signals the tracer hangs off. A rename would leave install()
    # silently connecting nothing, since it suppresses exceptions on
    # purpose so one missing signal cannot cost the whole trail.
    project = QgsProject.instance()
    check(
        "QgsProject has a readProject signal",
        lambda: _assert(
            hasattr(project, "readProject"),
            "no readProject signal; project loads would not be logged",
        ),
    )
    check(
        "QgsProject has a layerWasAdded signal",
        lambda: _assert(
            hasattr(project, "layerWasAdded"),
            "no layerWasAdded signal; layers would not be logged",
        ),
    )

    tracer = LoadTracer()
    check("LoadTracer.install() against a real project", tracer.install)
    check(
        "LoadTracer.install() is idempotent",
        lambda: tracer.install(),
    )
    check("LoadTracer.remove() disconnects cleanly", tracer.remove)
    check("LoadTracer.remove() is safe twice", tracer.remove)

    # A real portal layer source, through the real builders, must be
    # recognised as ours and reported as carrying its authcfg. A trail
    # that labels portal layers "other" points the next investigation at
    # the wrong half of the project.
    from gratisgis_qgis.browser.uris import authed_vector_tile_uri

    described = check(
        "describe_layer() on a real authed portal source",
        lambda: describe_layer(
            "Parcels",
            authed_vector_tile_uri(
                "https://portal.example", "item-1", "layer-1", authcfg_id="ab12cd3"
            ),
        ),
    )
    check(
        "an authed portal layer is reported as portal + authcfg",
        lambda: _assert(
            "portal" in str(described) and "authcfg=ab12cd3" in str(described),
            f"portal layer not recognised in the trail: {described!r}",
        ),
    )

    # The dump itself, written by the same code path a freeze uses.
    dump_dir = log_directory() / "smoke"
    dump_file = dump_dir / "freeze-smoke.txt"
    check("write_dump() writes a dump", lambda: _assert(
        write_dump(dump_file, 12.5), f"could not write a dump to {dump_file}"
    ))
    dump_text = check(
        "the dump is readable",
        lambda: dump_file.read_text(encoding="utf-8"),
    )
    check(
        "the dump carries a thread roster and stacks",
        lambda: _assert(
            "Threads by id" in str(dump_text)
            and "<- GUI thread" in str(dump_text)
            and "Current thread 0x" in str(dump_text),
            "the dump is missing the roster or the stacks",
        ),
    )
    # The roster's hex spelling must match what faulthandler printed in
    # the same file, or the table joins to nothing. The widths differ
    # between Windows and Linux, so this is asserted where it runs.
    check(
        "roster ids match the faulthandler spelling",
        lambda: _assert(
            _roster_ids_join(str(dump_text)),
            "the roster's hex ids do not appear in the stacks below them",
        ),
    )
    check("thread_roster() names the GUI thread", lambda: _assert(
        "<- GUI thread" in thread_roster(), "the main thread is not marked"
    ))
    with contextlib.suppress(Exception):
        dump_file.unlink()
        dump_dir.rmdir()

    # The heartbeat, under a real Qt event loop. If QTimer does not fire
    # the detector never ticks, and a watchdog that never ticks reports
    # a permanent stall instead of a real one.
    app = QCoreApplication.instance()
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    deadline = time.monotonic() + 3.0
    while len(ticks) < 3 and time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    timer.stop()
    check(
        "a QTimer heartbeat fires under a real event loop",
        lambda: _assert(
            len(ticks) >= 3,
            f"QTimer fired {len(ticks)} times in 3s; the watchdog would "
            "read a live GUI thread as permanently frozen",
        ),
    )

    # start() must never raise inside initGui, whatever the environment.
    watchdog = FreezeWatchdog(log_directory())
    check("FreezeWatchdog.start() does not raise", watchdog.start)
    check("FreezeWatchdog.stop() does not raise", watchdog.stop)
    check("FreezeWatchdog.stop() is safe twice", watchdog.stop)


def _roster_ids_join(dump_text: str) -> bool:
    """Every id in the roster must appear again in the stacks below.

    The roster is only useful if a reader can take an id from the table
    and find it heading a stack. Different padding on the two sides
    still looks like a working table and joins to nothing.
    """
    import re as _re

    head, _, tail = dump_text.partition("Python stacks for every thread")
    roster_ids = set(_re.findall(r"(0x[0-9a-f]+)", head))
    stack_ids = set(_re.findall(r"[Tt]hread (0x[0-9a-f]+)", tail))
    return bool(roster_ids) and roster_ids.issubset(stack_ids)


class _CollectingCombo:
    """The parts of QComboBox a dialog's combo-population method uses.

    Real enough for the production method, without needing a Qt widget
    tree in a headless run.
    """

    def __init__(self) -> None:
        self.items: list[tuple[str, object]] = []
        self.enabled = True

    def addItem(self, text: str, userData: object = None) -> None:  # Qt API name
        self.items.append((text, userData))

    def clear(self) -> None:  # Qt API name
        self.items.clear()

    def count(self) -> int:  # Qt API name
        return len(self.items)

    def setEnabled(self, value: bool) -> None:  # Qt API name
        self.enabled = value

    def setCurrentIndex(self, index: int) -> None:  # Qt API name
        self.current = index


def _run_one_task() -> None:
    """Push one task through the real QgsTask manager and wait."""
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QTimer

    from gratisgis_qgis.tasks import run_in_task

    loop = QEventLoop()
    outcome: dict[str, object] = {}

    def done(value: object) -> None:
        outcome["value"] = value
        loop.quit()

    def failed(exc: BaseException) -> None:
        outcome["error"] = exc
        loop.quit()

    run_in_task(
        "GratisGIS smoke task",
        lambda handle: (handle.set_progress(50.0), "done")[1],
        done,
        failed,
        cancelable=False,
    )

    # Bound the wait so a wedged task fails the run instead of hanging.
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(15_000)
    loop.exec()
    QCoreApplication.processEvents()
    QgsApplication.taskManager().cancelAll()

    if "error" in outcome:
        raise AssertionError(f"task reported an error: {outcome['error']!r}")
    if outcome.get("value") != "done":
        raise AssertionError(f"task did not complete, outcome={outcome!r}")


def _assert_decodes(
    registry: object, provider_key: str, uri: str, expect: dict | None = None
) -> dict:
    """The provider must parse our URI into the parts it expects."""
    parts = registry.decodeUri(provider_key, uri)  # type: ignore[attr-defined]
    if not parts:
        raise AssertionError(f"{provider_key} decoded {uri!r} to nothing")
    for key, want in (expect or {}).items():
        got = parts.get(key)
        if got != want:
            raise AssertionError(
                f"{provider_key} decoded {key}={got!r}, expected {want!r} (uri={uri!r})"
            )
    return parts


def _as_text(value) -> str:
    """Render a QGIS attribute as comparable text.

    QDateTime and friends do not compare equal to the ISO strings they
    were loaded from, so anything date-shaped goes through its own
    formatter first.
    """
    to_string = getattr(value, "toString", None)
    if callable(to_string):
        try:
            return str(to_string("yyyy-MM-ddTHH:mm:ss"))
        except TypeError:
            return str(to_string())
    return str(value)


def _assert(condition: bool, message: str) -> bool:
    if not condition:
        raise AssertionError(message)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
