# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writing the clone-origin table into the cloned GeoPackage.

The origin table is what makes an offline clone pushable, and the two
things that can silently ruin it are pinned here:

- the second write must use CreateOrOverwriteLayer. The default action
  for an existing file replaces the whole file, which would leave the
  user with a GeoPackage holding provenance and no data.
- both writes must target the same ``safe_write_path`` temp file, so a
  clone that fails halfway still cannot replace a good previous copy.

Real GDAL is not on the CI runner, so the write is exercised against a
recording QgsVectorFileWriter stub; the accompanying round-trip
through a real GeoPackage skips cleanly when GDAL is absent.
"""
from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest

from gratisgis_qgis.browser.uris import PortalLayerRef
from gratisgis_qgis.offline.clone import (
    CLONE_SOURCE_FIELDS,
    CLONE_SOURCE_TABLE,
    read_clone_source,
)
from tests.plugin.conftest import install_qgis_stub

_REF = PortalLayerRef(
    portal_url="https://portal.example", item_id="item-1", layer_id="trails"
)

_FC = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": None, "properties": {"name": "X"}}
    ],
}

_WIDGETS = [
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QFileDialog",
    "QFormLayout",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QMessageBox",
    "QProgressBar",
    "QPushButton",
    "QVBoxLayout",
    "QWidget",
]


# Both writer stubs report success with the same value the production
# resolver looks up under either enum spelling.
_NO_ERROR = 0


class _SaveVectorOptions:
    def __init__(self) -> None:
        self.driverName = ""
        self.fileEncoding = ""
        self.layerName = ""
        self.actionOnExistingFile: Any = None


class _RecordingWriter:
    """Stand-in for QgsVectorFileWriter with the QGIS 4 scoped enums."""

    calls: ClassVar[list[dict[str, Any]]] = []
    fail_on_layer: str | None = None

    class WriterError:
        NoError = _NO_ERROR

    class ActionOnExistingFile:
        CreateOrOverwriteFile = 0
        CreateOrOverwriteLayer = 1

    SaveVectorOptions = _SaveVectorOptions

    @classmethod
    def writeAsVectorFormatV3(  # QGIS API name
        cls, layer: Any, path: str, ctx: Any, options: _SaveVectorOptions
    ) -> tuple[int, str]:
        cls.calls.append(
            {
                "layer": layer,
                "path": path,
                "layer_name": options.layerName,
                "driver": options.driverName,
                "action": options.actionOnExistingFile,
            }
        )
        # A real writer creates the file at the path it is handed.
        # The fake must too: safe_write_path now yields a path that
        # does NOT exist yet (a temp file was previously pre-created by
        # mkstemp, which is exactly what stopped OGR from producing a
        # GeoPackage there), so promoting a never-written path must
        # fail loudly rather than be papered over by the fixture.
        pathlib.Path(path).touch()
        if cls.fail_on_layer == options.layerName:
            return (99, "writer said no")
        return (_NO_ERROR, "")


class _LegacyWriter(_RecordingWriter):
    """QGIS 3 exposed both enums as flat class attributes."""

    WriterError = None  # type: ignore[assignment]
    ActionOnExistingFile = None  # type: ignore[assignment]
    NoError = _NO_ERROR
    CreateOrOverwriteLayer = 1


class _FakeProvider:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.accept = True

    def addFeatures(self, features: list[Any]) -> tuple[bool, list[Any]]:
        # QGIS API name; returns (ok, features) on both QGIS 3 and 4.
        self.added.extend(features)
        return (self.accept, features)


class _FakeVectorLayer:
    """Records every layer the writer path constructs."""

    built: ClassVar[list[_FakeVectorLayer]] = []

    def __init__(self, uri: str, name: str, provider: str) -> None:
        self.uri = uri
        self.layer_name = name
        self.provider = provider
        self._provider = _FakeProvider()
        self.crs_set: Any = None
        type(self).built.append(self)

    def isValid(self) -> bool:  # QGIS API name
        return True

    def crs(self) -> Any:  # QGIS API name
        return SimpleNamespace(isValid=lambda: True)

    def setCrs(self, crs: Any) -> None:  # QGIS API name
        self.crs_set = crs

    def fields(self) -> Any:  # QGIS API name
        return SimpleNamespace(names=self.uri)

    def dataProvider(self) -> _FakeProvider:  # QGIS API name
        return self._provider


class _FakeFeature:
    def __init__(self, fields: Any) -> None:
        self.fields = fields
        self.attributes: list[Any] = []

    def setAttributes(self, values: list[Any]) -> None:  # QGIS API name
        self.attributes = list(values)


@pytest.fixture
def clone_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _RecordingWriter.calls = []
    _RecordingWriter.fail_on_layer = None
    _FakeVectorLayer.built = []
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {
                "QgsCoordinateReferenceSystem": type("QgsCRS", (), {}),
                "QgsCoordinateTransformContext": type("QgsCtx", (), {}),
                "QgsFeature": _FakeFeature,
                "QgsProject": type("QgsProject", (), {}),
                "QgsVectorFileWriter": _RecordingWriter,
                "QgsVectorLayer": _FakeVectorLayer,
                # Only imported so the module loads; the write path
                # here never inspects a layer's class.
                "QgsVectorTileLayer": type("QgsVectorTileLayer", (), {}),
            },
            "qgis.PyQt.QtCore": {
                "Qt": type("Qt", (), {}),
                "QSettings": type("QSettings", (), {}),
            },
            "qgis.PyQt.QtWidgets": {
                name: type(name, (), {}) for name in _WIDGETS
            },
        },
    )
    import gratisgis_qgis.ui.clone_dialog as mod

    # The module-level name was bound at first import, which may have
    # happened under a different test's stub set.
    monkeypatch.setattr(mod, "QgsVectorLayer", _FakeVectorLayer)
    return mod


def _write(mod: ModuleType, gpkg: Path, ref: PortalLayerRef | None) -> None:
    mod._write_geojson_to_geopackage(_FC, str(gpkg), source=ref)


class TestCloneSourceTableWrite:
    def test_writes_the_data_layer_then_the_origin_table(
        self, clone_mod: ModuleType, tmp_path: Path
    ) -> None:
        gpkg = tmp_path / "trails.gpkg"
        _write(clone_mod, gpkg, _REF)

        data_call, source_call = _RecordingWriter.calls
        assert data_call["layer_name"] == "trails"
        assert source_call["layer_name"] == CLONE_SOURCE_TABLE
        assert source_call["driver"] == "GPKG"

    def test_origin_table_is_added_not_substituted(
        self, clone_mod: ModuleType, tmp_path: Path
    ) -> None:
        # CreateOrOverwriteFile here would discard the feature layer
        # written a moment earlier and hand the user an empty clone.
        _write(clone_mod, tmp_path / "trails.gpkg", _REF)
        _data, source_call = _RecordingWriter.calls
        assert (
            source_call["action"]
            == _RecordingWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        )

    def test_both_writes_target_the_same_temp_file(
        self, clone_mod: ModuleType, tmp_path: Path
    ) -> None:
        gpkg = tmp_path / "trails.gpkg"
        _write(clone_mod, gpkg, _REF)
        data_call, source_call = _RecordingWriter.calls
        assert data_call["path"] == source_call["path"]
        # Written to the sibling temp file, promoted afterwards, so a
        # failure cannot destroy a previous clone.
        assert data_call["path"] != str(gpkg)
        assert gpkg.exists()
        assert list(tmp_path.glob("*.part")) == []

    def test_origin_row_carries_the_portal_coordinates(
        self, clone_mod: ModuleType, tmp_path: Path
    ) -> None:
        _write(clone_mod, tmp_path / "trails.gpkg", _REF)
        holder = _FakeVectorLayer.built[-1]
        assert holder.provider == "memory"
        assert holder.layer_name == CLONE_SOURCE_TABLE
        # Geometry-less: an origin row has no location, and a geometry
        # column would make QGIS list it as a map layer.
        assert holder.uri.startswith("None?")
        for name in CLONE_SOURCE_FIELDS:
            assert f"field={name}:string" in holder.uri

        [feature] = holder.dataProvider().added
        portal_url, item_id, layer_id, cloned_at = feature.attributes
        assert (portal_url, item_id, layer_id) == (
            _REF.portal_url,
            _REF.item_id,
            _REF.layer_id,
        )
        # Values are positional, so the order must match the shared
        # field tuple or the reader maps the wrong column.
        assert datetime.fromisoformat(cloned_at).tzinfo is not None

    def test_legacy_flat_enum_spelling_still_resolves(
        self, clone_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # QGIS 3.34 is still supported and exposes the writer enums as
        # flat class attributes.
        import qgis.core  # type: ignore[import-not-found]

        monkeypatch.setattr(qgis.core, "QgsVectorFileWriter", _LegacyWriter)
        _write(clone_mod, tmp_path / "trails.gpkg", _REF)
        _data, source_call = _RecordingWriter.calls
        assert source_call["action"] == _LegacyWriter.CreateOrOverwriteLayer

    def test_no_source_writes_only_the_data_layer(
        self, clone_mod: ModuleType, tmp_path: Path
    ) -> None:
        _write(clone_mod, tmp_path / "trails.gpkg", None)
        assert len(_RecordingWriter.calls) == 1

    def test_origin_table_failure_does_not_lose_the_clone(
        self, clone_mod: ModuleType, tmp_path: Path
    ) -> None:
        # The features are already downloaded and written; failing the
        # whole clone over a missing provenance row would throw away
        # work that may have taken minutes.
        _RecordingWriter.fail_on_layer = CLONE_SOURCE_TABLE
        gpkg = tmp_path / "trails.gpkg"
        _write(clone_mod, gpkg, _REF)
        assert gpkg.exists()

    def test_data_layer_failure_still_aborts_the_clone(
        self, clone_mod: ModuleType, tmp_path: Path
    ) -> None:
        _RecordingWriter.fail_on_layer = "trails"
        gpkg = tmp_path / "trails.gpkg"
        with pytest.raises(RuntimeError, match="GeoPackage write failed"):
            _write(clone_mod, gpkg, _REF)
        assert not gpkg.exists()


@pytest.mark.skipif(
    importlib.util.find_spec("osgeo") is None,
    reason="GDAL (osgeo) is not installed; needs a real GeoPackage container",
)
class TestRealGeoPackageRoundTrip:
    """The reader against a GeoPackage written by GDAL itself.

    QgsVectorFileWriter is a GDAL front end, so a table created here
    with the same name, fields and geometry-less type is the same
    thing the plugin writes at runtime.
    """

    def test_origin_row_survives_the_container(self, tmp_path: Path) -> None:
        from osgeo import ogr  # type: ignore[import-not-found]

        gpkg = str(tmp_path / "trails.gpkg")
        driver = ogr.GetDriverByName("GPKG")
        ds = driver.CreateDataSource(gpkg)
        layer = ds.CreateLayer(CLONE_SOURCE_TABLE, geom_type=ogr.wkbNone)
        for name in CLONE_SOURCE_FIELDS:
            layer.CreateField(ogr.FieldDefn(name, ogr.OFTString))
        feature = ogr.Feature(layer.GetLayerDefn())
        for name, value in zip(
            CLONE_SOURCE_FIELDS,
            [_REF.portal_url, _REF.item_id, _REF.layer_id, "2026-08-14T00:00:00+00:00"],
            strict=True,
        ):
            feature.SetField(name, value)
        layer.CreateFeature(feature)
        ds = None

        assert read_clone_source(gpkg) == _REF
