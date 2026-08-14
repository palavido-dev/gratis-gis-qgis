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

import os
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

    # A URI the provider cannot decode yields an empty layer with no
    # error dialog, which is the failure mode this whole authed-tile
    # path exists to remove. Decoding is offline: no tile is fetched.
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

        def overwrite_after_release():
            with safe_write_path(target) as tmp:
                write_gpkg(tmp)
            return True

        check("overwrite succeeds once the layer is removed", overwrite_after_release)
        check(
            "and still leaves nothing behind",
            lambda: _assert(
                os.listdir(work) == ["clone.gpkg"],
                f"leftovers after success: {os.listdir(work)}",
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
    # Theme icon names are not API. A renamed one yields a null icon,
    # which ships as an invisible toolbar button rather than an error,
    # so the only way to know is to ask a real QGIS.
    import re as _re

    from qgis.core import QgsApplication

    from gratisgis_qgis import plugin as plugin_mod

    with open(plugin_mod.__file__, encoding="utf-8") as fh:
        source = fh.read()
    icon_names = _re.findall(r'"(/m[A-Za-z0-9]+\.svg|/search\.svg)"', source)
    check(
        "the toolbar names some icons at all",
        lambda: _assert(
            len(icon_names) >= 6,
            f"expected at least 6 icon names, found {icon_names!r}",
        ),
    )
    for icon_name in icon_names:
        check(
            f"theme icon {icon_name}",
            lambda n=icon_name: _assert(
                not QgsApplication.getThemeIcon(n).isNull(),
                f"QGIS has no theme icon named {n}; the button would be blank",
            ),
        )
    check(
        "the icons are not all the same one",
        lambda: _assert(
            len(set(icon_names)) == len(icon_names),
            f"duplicate icons would make the buttons indistinguishable: {icon_names!r}",
        ),
    )

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
