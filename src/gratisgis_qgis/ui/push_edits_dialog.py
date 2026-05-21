# SPDX-License-Identifier: AGPL-3.0-or-later
"""Push-edits dialog (Phase 4).

QGIS-side wrapper around the pure-Python sync planner in
`edit/sync.py`. The dialog:

  1. Lists vector layers in the project that come from a portal
     source we recognize (OAPIF / vectortile).
  2. For each, walks the layer's `editBuffer()` to capture added,
     changed-geometry, changed-attribute, and deleted features.
  3. Builds a SyncPlan and shows it for confirmation.
  4. On Push, executes the plan against the portal's features
     endpoint, one HTTP call per SyncOp, reporting progress and
     per-op failures.

The push isn't QGIS's built-in "Save Edits" because not every
portal we recognize exposes WFS-T through the OAPIF provider.
Going through the portal's REST CRUD lets us push edits even
against read-only OAPIF endpoints (and is the path the offline-
sync flow in Phase 7 reuses).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.core import (  # type: ignore[import-not-found]
    QgsFeature,
    QgsJsonExporter,
    QgsMapLayer,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]
from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from gratisgis_client.endpoints.features import FeatureIn

from ..browser.fetch import _connected_client, _run
from ..browser.uris import parse_oapif_uri
from ..edit.sync import (
    EditedFeature,
    SyncOp,
    SyncPlan,
    build_sync_plan,
    summarize_plan,
)
from ..log import get_logger
from ..settings import ConnectionStore

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)

# QGIS feature-id property the OAPIF provider stamps on each row;
# it carries the portal feature uuid. We pull it as the portal_id
# for update/delete ops.
_PORTAL_ID_FIELDS = ("featureId", "feature_id", "id", "fid_portal")


class PushEditsDialog(QDialog):
    """Modal driving the push-edits flow."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Push edits to GratisGIS")
        self.setMinimumWidth(560)
        self._iface = iface
        self._store = ConnectionStore()
        self._plan: SyncPlan | None = None
        self._target_item_id: str | None = None
        self._target_layer_id: str | None = None
        self._target_profile_name: str | None = None

        self._layer_combo = QComboBox()
        self._populate_layer_combo()
        self._connection_combo = QComboBox()
        self._populate_connection_combo()
        self._layer_combo.currentIndexChanged.connect(self._on_layer_changed)

        form = QFormLayout()
        form.addRow("Layer:", self._layer_combo)
        form.addRow("Portal:", self._connection_combo)

        self._summary_label = QLabel("")
        self._ops_list = QListWidget()
        self._skipped_list = QListWidget()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Push")
        buttons.accepted.connect(self._on_push)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(self._summary_label)
        root.addWidget(QLabel("Operations to push:"))
        root.addWidget(self._ops_list, 2)
        root.addWidget(QLabel("Skipped (not pushable):"))
        root.addWidget(self._skipped_list, 1)
        root.addWidget(buttons)
        self.setLayout(root)

        self._on_layer_changed()

    # ----- Setup -----

    def _populate_layer_combo(self) -> None:
        # Only show vector layers whose source URI we recognize as a
        # portal endpoint. Pushing edits to an unrelated layer is
        # meaningless and would confuse the user.
        project = QgsProject.instance()
        for layer_id, layer in project.mapLayers().items():
            if layer.type() != QgsMapLayer.VectorLayer:
                continue
            assert isinstance(layer, QgsVectorLayer)
            parsed = parse_oapif_uri(layer.source())
            if parsed is None:
                continue
            self._layer_combo.addItem(layer.name(), userData=layer_id)
        if self._layer_combo.count() == 0:
            self._layer_combo.addItem("(no portal-backed layers in project)", None)
            self._layer_combo.setEnabled(False)

    def _populate_connection_combo(self) -> None:
        for name in self._store.list_names():
            profile = self._store.get(name)
            if profile is None or not profile.is_discovered:
                continue
            self._connection_combo.addItem(profile.display_label, userData=name)
        if self._connection_combo.count() == 0:
            self._connection_combo.addItem("(no signed-in connections)", None)
            self._connection_combo.setEnabled(False)

    # ----- Plan-build path -----

    def _on_layer_changed(self) -> None:
        self._plan = None
        self._ops_list.clear()
        self._skipped_list.clear()
        self._summary_label.setText("")

        layer = self._selected_layer()
        if layer is None:
            return
        # Resolve the portal item-id and layer-id from the OAPIF URI.
        parsed = parse_oapif_uri(layer.source())
        if parsed is None:
            self._summary_label.setText(
                "Selected layer doesn't appear to be a portal-backed OAPIF source."
            )
            return
        _portal_url, type_name = parsed
        # Portal collection-id is `<itemId>` or `<itemId>__<layerKey>`.
        if "__" in type_name:
            item_id, layer_id = type_name.split("__", 1)
        else:
            item_id, layer_id = type_name, "default"
        self._target_item_id = item_id
        self._target_layer_id = layer_id

        edits = _collect_edits(layer)
        plan = build_sync_plan(edits)
        self._plan = plan
        self._render_plan(plan)

    def _render_plan(self, plan: SyncPlan) -> None:
        self._summary_label.setText(summarize_plan(plan))
        for op in plan.ops:
            label = self._render_op(op)
            row = QListWidgetItem(label)
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
            self._ops_list.addItem(row)
        for s in plan.skipped:
            row = QListWidgetItem(f"qgis-fid {s.qgis_fid}: {s.reason}")
            row.setFlags(row.flags() & ~Qt.ItemIsSelectable)
            self._skipped_list.addItem(row)

    @staticmethod
    def _render_op(op: SyncOp) -> str:
        if op.kind == "create":
            geom = "with geom" if op.geometry else "no geom"
            return f"CREATE (qgis fid {op.qgis_fid}, {geom})"
        if op.kind == "update":
            parts: list[str] = []
            if op.geometry is not None:
                parts.append("geom")
            if op.properties:
                parts.append(f"{len(op.properties)} attr(s)")
            return f"UPDATE portal id {op.portal_id} ({', '.join(parts) or 'no-op'})"
        if op.kind == "delete":
            return f"DELETE portal id {op.portal_id}"
        return f"? {op.kind}"

    def _selected_layer(self) -> QgsVectorLayer | None:
        layer_id = self._layer_combo.currentData()
        if not layer_id:
            return None
        layer = QgsProject.instance().mapLayer(layer_id)
        return layer if isinstance(layer, QgsVectorLayer) else None

    # ----- Execute path -----

    def _on_push(self) -> None:
        if self._plan is None or not self._plan.ops:
            QMessageBox.information(
                self,
                "Nothing to push",
                "No portal-pushable operations in the selected layer's edit buffer.",
            )
            return
        profile_name = self._connection_combo.currentData()
        if not profile_name:
            QMessageBox.warning(
                self, "No connection selected", "Pick a signed-in connection."
            )
            return
        if not self._target_item_id or not self._target_layer_id:
            QMessageBox.critical(
                self,
                "Target unresolved",
                "Could not resolve the portal item from the layer source.",
            )
            return
        profile = self._store.get(profile_name)
        if profile is None or not profile.is_discovered:
            QMessageBox.warning(self, "Connection not ready", "Sign in first.")
            return
        self._target_profile_name = profile_name

        ok_button = self._buttons.button(QDialogButtonBox.Ok)
        ok_button.setEnabled(False)

        failures: list[tuple[SyncOp, str]] = []
        try:
            for i, op in enumerate(self._plan.ops, start=1):
                self._summary_label.setText(
                    f"Pushing op {i} of {len(self._plan.ops)} ({op.kind})..."
                )
                try:
                    _run(_apply_op(
                        profile=profile,
                        item_id=self._target_item_id,
                        layer_id=self._target_layer_id,
                        op=op,
                    ))
                except Exception as e:  # pragma: no cover -- defensive
                    _log.exception("op failed: %s", op)
                    failures.append((op, str(e)))
        finally:
            ok_button.setEnabled(True)

        if failures:
            details = "\n".join(
                f"- {f[0].kind} {f[0].portal_id or '(new)'}: {f[1]}"
                for f in failures
            )
            QMessageBox.warning(
                self,
                "Some operations failed",
                f"{len(failures)} of {len(self._plan.ops)} operations failed.\n\n"
                f"Details:\n{details}",
            )
            return
        QMessageBox.information(
            self,
            "Pushed",
            f"{len(self._plan.ops)} operations pushed successfully.",
        )
        self.accept()


# -----------------------------------------------------------
# QGIS edit-buffer extraction
# -----------------------------------------------------------


def _collect_edits(layer: QgsVectorLayer) -> list[EditedFeature]:
    """Walk a layer's edit buffer and produce EditedFeature records.

    We deliberately don't import qgis from the planner module; this
    helper bridges the two. Returns an empty list if the layer
    isn't in edit mode (the dialog still shows a 0-op plan).
    """
    buf = layer.editBuffer()
    if buf is None:
        return []

    exporter = QgsJsonExporter(layer)
    out: list[EditedFeature] = []

    # 1) Added features. fid is negative for unsaved adds.
    for fid, feat in buf.addedFeatures().items():
        out.append(
            EditedFeature(
                kind="create",
                portal_id=None,
                qgis_fid=int(fid),
                geometry=_geom_to_geojson(exporter, feat),
                properties=_props_to_dict(feat),
            )
        )

    # 2) Changed geometries.
    for fid, geom in buf.changedGeometries().items():
        feat = _lookup_feature(layer, int(fid))
        out.append(
            EditedFeature(
                kind="update",
                portal_id=_portal_id_from_feature(feat),
                qgis_fid=int(fid),
                geometry=_geom_to_geojson_from_geom(exporter, geom),
                properties=None,
            )
        )

    # 3) Changed attribute values. The buffer stores per-fid dicts
    # keyed by attribute index; we resolve them to field names.
    field_names = [f.name() for f in layer.fields()]
    for fid, attr_map in buf.changedAttributeValues().items():
        feat = _lookup_feature(layer, int(fid))
        props: dict = {}
        for idx, value in attr_map.items():
            try:
                name = field_names[int(idx)]
            except (IndexError, ValueError):
                continue
            props[name] = _coerce_attr_value(value)
        out.append(
            EditedFeature(
                kind="update",
                portal_id=_portal_id_from_feature(feat),
                qgis_fid=int(fid),
                geometry=None,
                properties=props,
            )
        )

    # 4) Deletions.
    for fid in buf.deletedFeatureIds():
        feat = _lookup_feature(layer, int(fid))
        out.append(
            EditedFeature(
                kind="delete",
                portal_id=_portal_id_from_feature(feat),
                qgis_fid=int(fid),
            )
        )

    return out


def _lookup_feature(layer: QgsVectorLayer, fid: int) -> QgsFeature | None:
    request = layer.getFeature(fid)
    if request is None or not request.isValid():
        return None
    return request


def _portal_id_from_feature(feat: QgsFeature | None) -> str | None:
    if feat is None:
        return None
    for name in _PORTAL_ID_FIELDS:
        try:
            value = feat[name]
        except (KeyError, IndexError):
            continue
        if value is None:
            continue
        return str(value)
    return None


def _geom_to_geojson(exporter: QgsJsonExporter, feat: QgsFeature) -> dict | None:
    if feat is None or not feat.hasGeometry():
        return None
    geom = feat.geometry()
    if geom.isEmpty():
        return None
    return _geom_to_geojson_from_geom(exporter, geom)


def _geom_to_geojson_from_geom(exporter: QgsJsonExporter, geom) -> dict | None:
    # QgsJsonExporter.exportFeature would also include properties; we
    # just want the geometry, so call exportGeometry directly.
    import json

    raw = geom.asJson()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _props_to_dict(feat: QgsFeature) -> dict:
    out: dict = {}
    if feat is None:
        return out
    fields = feat.fields()
    for i in range(fields.count()):
        out[fields[i].name()] = _coerce_attr_value(feat.attribute(i))
    return out


def _coerce_attr_value(value):
    # QVariant types come back as native Python in PyQt5/6 most of
    # the time, but NULL and Date objects need a normalization pass.
    if value is None:
        return None
    # QVariant NULL stringifies as 'NULL' but the safer check is
    # the absence of a meaningful __bool__ on the typed value.
    s = str(value)
    if s == "NULL":
        return None
    return value


# -----------------------------------------------------------
# Async bridges (one HTTP call per op; sequenced by the caller).
# -----------------------------------------------------------


async def _apply_op(*, profile, item_id: str, layer_id: str, op: SyncOp) -> None:
    async with _connected_client(profile) as client:
        if op.kind == "create":
            await client.features.append(
                item_id=item_id,
                layer_id=layer_id,
                features=[
                    FeatureIn(geometry=op.geometry, properties=op.properties)
                ],
            )
            return
        if op.kind == "update":
            assert op.portal_id is not None
            await client.features.update(
                item_id=item_id,
                layer_id=layer_id,
                feature_id=op.portal_id,
                geometry=op.geometry,
                properties=op.properties,
            )
            return
        if op.kind == "delete":
            assert op.portal_id is not None
            await client.features.delete(
                item_id=item_id,
                layer_id=layer_id,
                feature_id=op.portal_id,
            )
            return
        raise AssertionError(f"unknown op kind: {op.kind!r}")
