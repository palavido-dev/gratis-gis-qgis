# SPDX-License-Identifier: AGPL-3.0-or-later
"""Send an offline clone's changes back to the portal.

The dialog lists the project layers that have somewhere to send
changes to, works out what changed, checks whether anyone else moved
the same features, and sends the result one call at a time.

Where "what changed" comes from depends on the layer, and the split is
forced by what each kind of layer is:

  - An **offline clone** is a GeoPackage this plugin wrote, carrying a
    baseline of how every feature looked when it was cloned. The
    difference between the file and that baseline is the pending work.
    That makes it durable: it survives saving, closing QGIS, reopening
    next week, even being emailed to someone else, because it is all
    inside the one file.
  - A **live OAPIF layer** has no local file to baseline against, so
    QGIS's pending edit buffer is the only record that exists. That
    path keeps its original behaviour, and its original limitation.

The clone path deliberately reads SAVED state only. The first version
of this dialog read the edit buffer for everything, which was wrong in
a way that mattered: it meant edits were invisible unless left unsaved
(so the ordinary habit of saving through a long session broke it), and
it made it possible to push and then answer "discard" in QGIS, leaving
the portal holding changes the local file never had with nothing aware
the two had diverged.

Conflicts are detected, not merged. Before sending, the portal is read
back and each feature's ``_edited_at`` compared against the value
recorded at clone time; anything that moved on both sides is named and
the user picks a side. That is the honest limit of what is possible:
the portal accepts no version token on a write and has no
compare-and-set, so nothing can make the write itself conditional. A
merge UI would imply a safety the server cannot provide.

Sending goes through the portal's REST CRUD rather than QGIS's own
Save Edits because not every portal exposes WFS-T through the OAPIF
provider.

The module name is historical; it predates this being a sync rather
than a one-way push.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qgis.core import (  # type: ignore[import-not-found]
    QgsFeature,
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

from gratisgis_client.client import GratisGISClient
from gratisgis_client.endpoints.features import FeatureIn

from ..browser.uris import PortalLayerRef, parse_oapif_layer_source
from ..edit.sync import (
    EditedFeature,
    SyncOp,
    SyncPlan,
    build_sync_plan,
    summarize_plan,
)
from ..log import get_logger
from ..offline.clone import (
    PORTAL_ID_PROPERTY,
    has_baseline,
    read_baseline,
    read_clone_source,
    write_baseline,
)
from ..offline.reader import (
    baseline_from_features,
    portal_edited_stamps,
    read_local_features,
)
from ..offline.sync_state import Conflict, find_conflicts, plan_local_changes
from ..portal import get_client
from ..settings import ConnectionStore
from ..tasks import TaskHandle, format_error, run_in_task

if TYPE_CHECKING:
    from qgis.gui import QgisInterface  # type: ignore[import-not-found]

_log = get_logger(__name__)

# Property names that may carry the portal feature uuid on a row.
# The clone flow's canonical column comes first (it is also the one
# the post-push write-back fills for created features); the rest are
# the id spellings the OAPIF provider / GeoJSON responses have used.
_PORTAL_ID_FIELDS = (PORTAL_ID_PROPERTY, "featureId", "feature_id", "id", "fid_portal")

# Shown when nothing in the project can be synced. Naming the fix is
# the point: the Browser tree hands out vector-tile layers for spatial
# data, which QGIS cannot edit at all, so a user staring at a portal
# layer in their Layers panel has no way to guess why this dialog
# claims there is nothing to send.
_NO_PUSHABLE_LAYERS = (
    "Nothing in this project can be synced. A layer added from the "
    "Browser tree draws as vector tiles, which are read only in QGIS. "
    "Use 'Clone layer for offline use...' on it first, then edit the "
    "offline copy and sync that."
)


class PushEditsDialog(QDialog):
    """Modal driving the push-edits flow."""

    def __init__(self, iface: QgisInterface, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sync layer with GratisGIS")
        self.setMinimumWidth(560)
        self._iface = iface
        self._store = ConnectionStore()
        self._plan: SyncPlan | None = None
        self._target_item_id: str | None = None
        self._target_layer_id: str | None = None
        self._target_profile_name: str | None = None
        self._push_task = None

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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Sync")
        buttons.accepted.connect(self._on_push)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(self._summary_label)
        root.addWidget(QLabel("Changes to send:"))
        root.addWidget(self._ops_list, 2)
        root.addWidget(QLabel("Skipped:"))
        root.addWidget(self._skipped_list, 1)
        root.addWidget(buttons)
        self.setLayout(root)

        self._on_layer_changed()

    # ----- Setup -----

    def _populate_layer_combo(self) -> None:
        # Only show layers we can actually push: a live OAPIF layer,
        # or an offline clone that recorded where it came from.
        # isinstance covers both QGIS 3 (where layer.type() returns
        # QgsMapLayer.VectorLayer as int) and QGIS 4 (where it
        # returns Qgis.LayerType.Vector).
        project = QgsProject.instance()
        for layer_id, layer in project.mapLayers().items():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if _resolve_push_target(layer) is None:
                continue
            self._layer_combo.addItem(layer.name(), userData=layer_id)
        if self._layer_combo.count() == 0:
            self._layer_combo.addItem("(no editable portal layers in project)", None)
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
            if not self._layer_combo.isEnabled():
                self._summary_label.setText(_NO_PUSHABLE_LAYERS)
            return
        ref = _resolve_push_target(layer)
        if ref is None:
            self._summary_label.setText(_NO_PUSHABLE_LAYERS)
            return
        self._target_item_id = ref.item_id
        self._target_layer_id = ref.layer_id

        edits, note = _collect_changes(layer)
        if note:
            self._summary_label.setText(note)
            return
        plan = build_sync_plan(edits)
        self._plan = plan
        self._render_plan(plan)

    def _render_plan(self, plan: SyncPlan) -> None:
        self._summary_label.setText(summarize_plan(plan))
        for op in plan.ops:
            label = self._render_op(op)
            row = QListWidgetItem(label)
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._ops_list.addItem(row)
        for s in plan.skipped:
            row = QListWidgetItem(f"qgis-fid {s.qgis_fid}: {s.reason}")
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
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

        ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setEnabled(False)
        self._layer_combo.setEnabled(False)
        self._connection_combo.setEnabled(False)

        # Snapshot everything the worker needs; the ops are pure data
        # so the whole loop runs in ONE task with per-op progress and
        # a working cancel. One call still equals one op server-side.
        # The layer reference is captured on the GUI thread for the
        # post-push id write-back, which also runs on the GUI thread.
        ops = list(self._plan.ops)
        item_id = self._target_item_id
        layer_id = self._target_layer_id
        layer = self._selected_layer()
        gpkg_path = _gpkg_path_from_source(layer.source()) if layer else None
        is_clone = bool(gpkg_path) and read_clone_source(gpkg_path or "") is not None

        if is_clone and gpkg_path:
            # Record every minted id locally BEFORE anything is sent.
            # If the run dies halfway, or a response is lost, the ids
            # are already in the file, so re-syncing resends the same
            # ids and the portal's dedupe turns the retry into a no-op
            # instead of a duplicate row. Doing this afterwards would
            # make a lost response indistinguishable from a rejection.
            if not _stamp_minted_ids(layer, ops):
                QMessageBox.critical(
                    self,
                    "Could not prepare the sync",
                    "The new features could not be given their ids in the "
                    "local file, so sending them now could create "
                    "duplicates on a retry. Nothing was sent.",
                )
                ok_button.setEnabled(True)
                self._layer_combo.setEnabled(True)
                self._connection_combo.setEnabled(True)
                return

            conflicts = self._check_conflicts(profile, ops, item_id, layer_id, gpkg_path)
            if conflicts is None:
                ok_button.setEnabled(True)
                self._layer_combo.setEnabled(True)
                self._connection_combo.setEnabled(True)
                return
            if conflicts:
                ops = self._resolve_conflicts(ops, conflicts)
                if ops is None:
                    ok_button.setEnabled(True)
                    self._layer_combo.setEnabled(True)
                    self._connection_combo.setEnabled(True)
                    return
                if not ops:
                    QMessageBox.information(
                        self,
                        "Nothing left to send",
                        "Every change you had was in conflict and you chose "
                        "to keep the portal's version.",
                    )
                    ok_button.setEnabled(True)
                    self._layer_combo.setEnabled(True)
                    self._connection_combo.setEnabled(True)
                    return

        def push_all(handle: TaskHandle) -> _PushOutcome:
            client = get_client(profile)
            failures: list[tuple[SyncOp, str]] = []
            created_ids: list[tuple[int, str]] = []
            attempted = 0
            for i, op in enumerate(ops, start=1):
                if handle.is_canceled():
                    # Return normally so the summary can report how
                    # far the push got; ops already sent stay sent.
                    return _PushOutcome(failures, attempted, cancelled=True, created_ids=created_ids)
                try:
                    new_id = _apply_op(client, item_id=item_id, layer_id=layer_id, op=op)
                except Exception as e:  # pragma: no cover - defensive
                    _log.exception("op failed: %s", op)
                    failures.append((op, format_error(e)))
                else:
                    if op.kind == "create" and new_id is not None and op.qgis_fid is not None:
                        created_ids.append((op.qgis_fid, new_id))
                attempted = i
                handle.set_progress(i * 100.0 / len(ops))
            return _PushOutcome(failures, attempted, cancelled=False, created_ids=created_ids)

        def done(outcome: _PushOutcome) -> None:
            self._push_task = None
            ok_button.setEnabled(True)
            self._layer_combo.setEnabled(True)
            self._connection_combo.setEnabled(True)
            # Before anything else: stamp the portal-assigned ids on
            # the pushed creates so a re-push updates instead of
            # duplicating. Best-effort; layers without the column
            # just log.
            _write_back_created_ids(layer, outcome.created_ids)
            self._summary_label.setText(summarize_plan(self._plan) if self._plan else "")
            if outcome.cancelled:
                QMessageBox.warning(
                    self,
                    "Sync cancelled",
                    f"Stopped after {outcome.attempted} of {len(ops)} operations. "
                    f"Operations already sent are on the portal; "
                    f"{len(outcome.failures)} of them failed.",
                )
                return
            if outcome.failures:
                details = "\n".join(
                    f"- {f[0].kind} {f[0].portal_id or '(new)'}: {f[1]}"
                    for f in outcome.failures
                )
                QMessageBox.warning(
                    self,
                    "Some operations failed",
                    f"{len(outcome.failures)} of {len(ops)} operations failed.\n\n"
                    f"Details:\n{details}",
                )
                return
            if is_clone and gpkg_path:
                # Only now is the local copy in step with the portal,
                # so only now may the baseline move. Doing it before
                # the send, or after a partial one, would mark unsent
                # work as already synced and lose it silently.
                self._refresh_baseline(profile, layer, gpkg_path, item_id, layer_id)
            QMessageBox.information(
                self,
                "Synced",
                f"{len(ops)} change(s) sent to the portal.",
            )
            self.accept()

        def failed(exc: BaseException) -> None:  # pragma: no cover - defensive
            self._push_task = None
            _log.error("push failed", exc_info=exc)
            ok_button.setEnabled(True)
            self._layer_combo.setEnabled(True)
            self._connection_combo.setEnabled(True)
            QMessageBox.critical(self, "Sync failed", format_error(exc))

        def progress(pct: float) -> None:
            done_ops = round(pct * len(ops) / 100.0)
            self._summary_label.setText(
                f"Pushing operation {min(done_ops + 1, len(ops))} of {len(ops)}..."
            )

        self._summary_label.setText(f"Pushing operation 1 of {len(ops)}...")
        self._push_task = run_in_task(
            "GratisGIS: push edits", push_all, done, failed, on_progress=progress
        )

    # ----- Conflicts -----

    def _check_conflicts(self, profile, ops, item_id, layer_id, gpkg_path):
        """Ask the portal whether anything moved under us.

        Returns the conflicts (possibly empty), or None if the check
        itself failed and the user chose not to continue.

        Run synchronously, unlike the push loop, because it is one
        request and its answer decides whether the push happens at
        all. Doing it in the background would mean either blocking the
        dialog anyway or letting the user press Sync twice.
        """
        try:
            body = get_client(profile).features.download_geojson(
                item_id=item_id, layer_id=layer_id
            )
            stamps = portal_edited_stamps(body)
        except Exception as e:
            _log.exception("conflict check failed")
            answer = QMessageBox.question(
                self,
                "Could not check the portal",
                "The portal could not be read to check whether anyone else "
                f"changed these features.\n\n{format_error(e)}\n\n"
                "Send your changes anyway?",
            )
            return [] if answer == QMessageBox.StandardButton.Yes else None

        changes = [
            EditedFeature(
                kind=op.kind,
                portal_id=op.portal_id,
                qgis_fid=op.qgis_fid,
                geometry=op.geometry,
                properties=op.properties,
            )
            for op in ops
        ]
        return find_conflicts(changes, read_baseline(gpkg_path), stamps)

    def _resolve_conflicts(self, ops, conflicts):
        """Let the user decide what happens to conflicting rows.

        Returns the ops to send, or None to abandon the sync.

        Two choices only, and no merge. The portal accepts no version
        token on a write, so there is no way to write "only if it is
        still what I read"; a merge UI would imply a safety the server
        cannot provide. Naming the rows and letting the user pick a
        side is the honest limit of what can be offered here.
        """
        detail = conflict_summary(conflicts)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Changed on the portal too")
        box.setText(
            f"{len(conflicts)} of your changes affect features that someone "
            "else has changed on the portal since you cloned them."
        )
        box.setInformativeText(
            f"{detail}\n\nSending yours will overwrite theirs. There is no "
            "way to merge the two automatically."
        )
        keep_mine = box.addButton("Overwrite with mine", QMessageBox.ButtonRole.DestructiveRole)
        keep_theirs = box.addButton("Skip those, send the rest", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is keep_mine:
            return ops
        if clicked is keep_theirs:
            return ops_without_conflicts(ops, conflicts)
        return None

    def _refresh_baseline(self, profile, layer, gpkg_path, item_id, layer_id):
        """Re-record the clone's baseline after a successful sync.

        Without this every change would be sent again on the next sync:
        the baseline still describes the pre-edit state, so the same
        rows keep reading as changed. Best-effort, and loudly logged if
        it fails, because a stale baseline costs a duplicate send that
        the portal's dedupe absorbs, whereas failing the whole sync
        after the changes have landed would be worse.
        """
        try:
            layer.reload()
            body = get_client(profile).features.download_geojson(
                item_id=item_id, layer_id=layer_id
            )
            write_baseline(
                gpkg_path,
                baseline_from_features(
                    read_local_features(layer), portal_edited_stamps(body)
                ),
            )
        except Exception:
            _log.exception("could not refresh the clone baseline")

    def reject(self) -> None:  # Qt override
        # Cancel button / Esc / window close during a push: stop the
        # op loop at the next boundary rather than letting it finish
        # headless behind a closed dialog.
        if self._push_task is not None:
            self._push_task.cancel()
        super().reject()


# -----------------------------------------------------------
# Which project layers can be pushed
# -----------------------------------------------------------


#: How many conflicting rows to name before summarising the rest.
#:
#: Enough to recognise what happened, few enough that the dialog stays
#: readable. A list of 400 rows in a message box is a wall the user
#: dismisses without reading, which is the opposite of the point.
CONFLICT_DETAIL_LIMIT = 10


def conflict_summary(
    conflicts: list[Conflict], limit: int = CONFLICT_DETAIL_LIMIT
) -> str:
    """The bulleted list of conflicting rows shown to the user.

    Split out of the message box so the truncation can be tested. It is
    the sort of thing that reads as cosmetic and is not: a user
    deciding whether to overwrite someone else's work is deciding from
    this text, and silently showing ten of four hundred conflicts would
    make a large collision look like a small one.
    """
    lines = [f"- {c.detail}" for c in conflicts[:limit]]
    remaining = len(conflicts) - limit
    if remaining > 0:
        lines.append(f"- and {remaining} more")
    return "\n".join(lines)


def ops_without_conflicts(
    ops: list[SyncOp], conflicts: list[Conflict]
) -> list[SyncOp]:
    """The ops to send when the user chooses to keep the portal's copy.

    Drops exactly the conflicting features and keeps everything else,
    which is the whole meaning of "skip those, send the rest". Getting
    it wrong in either direction is silent: drop too much and the
    user's other edits never arrive, drop too little and the overwrite
    they declined happens anyway.
    """
    conflicted = {c.global_id for c in conflicts}
    return [op for op in ops if op.portal_id not in conflicted]


def _resolve_push_target(layer: QgsVectorLayer) -> PortalLayerRef | None:
    """Resolve the portal layer a project layer's edits belong to.

    Two shapes qualify, and the asymmetry with the clone dialog is
    deliberate:

      - a live OAPIF layer, which QGIS can put into edit mode;
      - an offline clone, which is an ordinary GeoPackage on disk
        carrying the origin table the clone flow wrote.

    Vector-tile layers are excluded even though they are portal
    layers, because QGIS treats vector tiles as a read-only
    rendering format: they have no edit buffer, so listing one here
    could only ever produce an empty plan.
    """
    source = layer.source()
    ref = parse_oapif_layer_source(source)
    if ref is not None:
        return ref
    gpkg_path = _gpkg_path_from_source(source)
    if gpkg_path is None:
        return None
    return read_clone_source(gpkg_path)


def _gpkg_path_from_source(source: str) -> str | None:
    """Pull the file path out of an OGR GeoPackage layer source.

    QGIS spells these ``<path>.gpkg|layername=<name>`` (the suffix is
    absent for single-layer files). Keyed on the extension rather
    than the provider name so it holds however the layer was added.
    """
    if not source:
        return None
    path = source.split("|", 1)[0].strip()
    return path if path.lower().endswith(".gpkg") else None


# -----------------------------------------------------------
# Working out what changed
# -----------------------------------------------------------


def _collect_changes(layer: QgsVectorLayer) -> tuple[list[EditedFeature], str]:
    """Find a layer's pending changes, by whichever route suits it.

    Returns the changes and, if the layer cannot be read right now, a
    sentence explaining why instead.

    Two routes, and the split is forced by what each kind of layer
    actually is:

      - An offline clone is a GeoPackage the plugin wrote, so it
        carries a baseline of how every feature looked when it was
        cloned. Comparing the file against that baseline is what makes
        the changes DURABLE: they survive saving, closing QGIS, and
        reopening next week, because they live in the file rather than
        in a UI buffer.
      - A live OAPIF layer has no local file, so there is nothing to
        baseline against and QGIS's pending edit buffer is the only
        record that exists. That path keeps its original behaviour and
        its original limitation.

    The clone route deliberately reads only SAVED state. That is the
    fix for the flaw the buffer version had: it could push edits that
    were still unsaved, after which answering "discard" in QGIS left
    the portal holding changes the local file never had, with nothing
    aware the two had diverged.
    """
    gpkg_path = _gpkg_path_from_source(layer.source())
    if gpkg_path is None or read_clone_source(gpkg_path) is None:
        return _collect_edits(layer), ""

    if not has_baseline(gpkg_path):
        return [], (
            "This copy was made by an older version of the plugin, which "
            "did not record what it started from. Clone the layer again "
            "to sync it."
        )
    if _layer_has_unsaved_edits(layer):
        return [], (
            "This layer has unsaved edits. Save them first (the pencil, "
            "then Save Layer Edits), then reopen this window. Only saved "
            "work is sent, so that discarding edits afterwards can never "
            "leave the portal out of step with your copy."
        )
    baseline = read_baseline(gpkg_path)
    return plan_local_changes(read_local_features(layer), baseline), ""


def _layer_has_unsaved_edits(layer: QgsVectorLayer) -> bool:
    try:
        return bool(layer.isModified())
    except AttributeError:
        return False


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

    out: list[EditedFeature] = []

    # 1) Added features. fid is negative for unsaved adds. A feature
    # that was added locally AND already pushed once carries the
    # portal id the last push wrote back into its portal-id column;
    # send those as updates so pushing twice before a commit cannot
    # duplicate them server-side.
    for fid, feat in buf.addedFeatures().items():
        existing_id = _portal_id_from_feature(feat)
        out.append(
            EditedFeature(
                kind="create" if existing_id is None else "update",
                portal_id=existing_id,
                qgis_fid=int(fid),
                geometry=_geom_to_geojson(feat),
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
                geometry=_geom_to_geojson_from_geom(geom),
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


def _geom_to_geojson(feat: QgsFeature) -> dict | None:
    if feat is None or not feat.hasGeometry():
        return None
    geom = feat.geometry()
    if geom.isEmpty():
        return None
    return _geom_to_geojson_from_geom(geom)


def _geom_to_geojson_from_geom(geom) -> dict | None:
    # QgsGeometry.asJson() serializes just the geometry, which is
    # exactly the GeoJSON fragment the portal's feature CRUD takes.
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
        name = fields[i].name()
        if name == PORTAL_ID_PROPERTY:
            # Local bookkeeping column (which feature this row IS on
            # the portal), not layer data; pushing it would smuggle a
            # plugin-internal property into the portal's row.
            continue
        out[name] = _coerce_attr_value(feat.attribute(i))
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
# Worker-side op execution (one HTTP call per op; the task loop
# above sequences them).
# -----------------------------------------------------------


@dataclass(frozen=True)
class _PushOutcome:
    """What the push task hands back to the GUI callback."""

    failures: list[tuple[SyncOp, str]]
    attempted: int
    cancelled: bool
    created_ids: list[tuple[int, str]] = field(default_factory=list)
    """(qgis fid, portal feature id) for every successfully pushed
    create, so the GUI callback can write the ids back into the
    layer's portal-id column."""


def _apply_op(
    client: GratisGISClient, *, item_id: str, layer_id: str, op: SyncOp
) -> str | None:
    """Execute one op against the portal.

    Returns the portal-assigned feature id for creates (the append
    response's ``globalIds`` is order-aligned with the request, and
    each create sends exactly one feature), ``None`` for everything
    else.
    """
    if op.kind == "create":
        result = client.features.append(
            item_id=item_id,
            layer_id=layer_id,
            features=[
                # The id travels with the create when the caller has
                # one. A clone mints it and records it locally BEFORE
                # sending, which is what makes a retry safe: the portal
                # dedupes an append on globalId, so resending after a
                # lost response cannot produce a second copy. Sending
                # None leaves the portal to assign one, which is still
                # right for the live-OAPIF path where there is no local
                # file to have recorded anything in.
                FeatureIn(
                    global_id=op.portal_id,
                    geometry=op.geometry,
                    properties=op.properties,
                )
            ],
        )
        if result.global_ids:
            return result.global_ids[0]
        return op.portal_id
    if op.kind == "update":
        if op.portal_id is None:
            # The planner never emits an update without one; failing
            # loudly beats PATCHing an empty id and misreporting sync.
            raise ValueError("update op without a portal id")
        client.features.update(
            item_id=item_id,
            layer_id=layer_id,
            feature_id=op.portal_id,
            geometry=op.geometry,
            properties=op.properties,
        )
        return None
    if op.kind == "delete":
        if op.portal_id is None:
            raise ValueError("delete op without a portal id")
        client.features.delete(
            item_id=item_id,
            layer_id=layer_id,
            feature_id=op.portal_id,
        )
        return None
    raise AssertionError(f"unknown op kind: {op.kind!r}")


def _stamp_minted_ids(
    layer: QgsVectorLayer | None, ops: list[SyncOp]
) -> bool:
    """Write each create's minted id into the clone before sending.

    Returns False if the write could not be made, in which case the
    caller must not send: an unrecorded id turns a retry into a
    duplicate, which is the one failure mode the whole minting scheme
    exists to prevent.

    A no-op when there is nothing to stamp, or when every create
    already carries its id from a previous attempt, which is the
    normal state on a retry.
    """
    pending = [
        op
        for op in ops
        if op.kind == "create" and op.portal_id and op.qgis_fid is not None
    ]
    if not pending:
        return True
    if layer is None:
        return False
    try:
        index = layer.fields().indexOf(PORTAL_ID_PROPERTY)
        if index < 0:
            # A clone always has this column; its absence means this is
            # not the file we think it is, so refuse rather than invent
            # a column on someone's layer.
            _log.warning("clone layer has no %r column", PORTAL_ID_PROPERTY)
            return False
        if not layer.startEditing():
            return False
        for op in pending:
            layer.changeAttributeValue(op.qgis_fid, index, op.portal_id)
        if not layer.commitChanges():
            _log.warning("could not commit minted ids: %s", layer.commitErrors())
            layer.rollBack()
            return False
    except Exception:
        _log.exception("could not stamp minted ids")
        return False
    return True


def _write_back_created_ids(
    layer: QgsVectorLayer | None, created: list[tuple[int, str]]
) -> None:
    """Stamp portal-assigned ids onto pushed creates, best-effort.

    Without this, the pushed features stay id-less in the local edit
    buffer and a second Push (or the offline-sync retry path) sends
    them as fresh creates, duplicating rows server-side. The write
    targets the clone flow's canonical portal-id column; layers
    without that column (the common live-OAPIF case, whose schema is
    the portal layer's own fields) just log, because inventing a
    column on someone's layer is worse than an informed no-op.
    Runs on the GUI thread, the only place a live layer may be
    touched.
    """
    if not created:
        return
    if layer is None:
        _log.info("created-id write-back skipped: layer no longer available")
        return
    try:
        idx = layer.fields().indexOf(PORTAL_ID_PROPERTY)
    except Exception:  # pragma: no cover - defensive
        _log.exception("created-id write-back: fields() lookup failed")
        return
    if idx < 0:
        _log.info(
            "Layer has no %r column; %d created feature id(s) were not "
            "written back (pushing again before committing would "
            "re-create them)",
            PORTAL_ID_PROPERTY,
            len(created),
        )
        return
    for fid, portal_id in created:
        try:
            ok = bool(layer.changeAttributeValue(fid, idx, portal_id))
        except Exception:  # pragma: no cover - defensive
            _log.exception("created-id write-back failed for qgis fid %s", fid)
            continue
        if not ok:
            _log.warning("created-id write-back rejected for qgis fid %s", fid)
