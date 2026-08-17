# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish one exported GeoPackage as a portal data_layer, end to end.

The vector publish used to live inside ``PublishLayerDialog`` as a
chain of nested callbacks: export, then a task to stage, then a GUI hop
to build the envelope, then a task to create and enqueue, then a timer
to poll. Every step was reachable only by opening the dialog and
completing the one before it, so none of it could be tested and none of
it could be reused.

That second part is the pressing one. Publishing a project as a map
needs to publish layers that are not on the portal yet, and it cannot
drive another dialog's private methods to do it. This module is the
callable version, shaped after ``run_raster_pipeline``.

Qt-free on purpose, like its raster counterpart. ``publish/vector.py``
holds the schema translation and imports no QGIS; this holds the
sequence of portal calls and imports no Qt. What is left in the dialog
is widgets, which is what a dialog should be.

The export is passed in as a callable rather than performed here. It
needs QGIS, and this module is deliberately QGIS-free so it can be
tested against a fake client with nothing installed. Passing it in also
means it runs on the worker thread with everything else: writing a
1.4M-feature layer to a GeoPackage takes long enough to freeze the QGIS
window, and it used to do exactly that before the first progress bar
appeared, so the plugin looked hung at the moment the user pressed the
button.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..log import get_logger
from ..portal import get_client
from ..tasks import TaskCancelledError, TaskHandle
from .vector import build_data_layer_envelope, layer_from_probe

if TYPE_CHECKING:
    from gratisgis_client.client import GratisGISClient

_log = get_logger(__name__)

# Progress budget. Exporting and staging are where the wall-clock
# goes, so they take the wide bands; the two metadata calls after
# them are quick and mostly exist to move the label on.
PCT_EXPORTED = 30.0
PCT_STAGED = 70.0
PCT_ITEM_CREATED = 85.0
PCT_ENQUEUED = 100.0

#: Progress at or below which the caller should still say "exporting".
#: Exported so the dialog's labels and this module's bands cannot drift
#: apart silently, which is the usual fate of a magic number copied
#: into a second file.
PCT_EXPORT_DONE = PCT_EXPORTED

#: And the same for the upload.
PCT_UPLOAD_DONE = PCT_STAGED


class NoLayersProbed(RuntimeError):
    """The portal read the uploaded file and found nothing in it.

    Its own type because it is the one failure here that is the user's
    to fix rather than ours to report: an empty or unreadable layer,
    usually invalid geometry. Everything else in this pipeline is a
    transport or portal error.
    """


@dataclass(frozen=True)
class VectorPublishOutcome:
    """What the pipeline hands back to the GUI callback."""

    item_id: str
    layer_id: str
    job: Any
    """The enqueued ``ImportJob``; the dialog polls it for progress.
    With several layers this is the FIRST job, kept so the dialog's
    single-job polling keeps working unchanged."""

    layer_ids: tuple[str, ...] = ()
    """Every layer created on the item, in stack order. Single-layer
    publishes carry a one-element tuple."""

    jobs: tuple[Any, ...] = ()
    """Every enqueued job, matching ``layer_ids`` by index."""


def run_vector_pipeline(
    handle: TaskHandle,
    *,
    profile: Any,
    export: Callable[[], str],
    title: str,
    description: str | None,
    access: str,
    cleanup_notes: list[str],
    delete_gpkg: bool = True,
) -> VectorPublishOutcome:
    """Export, stage, probe, create the item, enqueue. In order.

    Raises on any hard failure; the caller's error callback renders it.
    ``cleanup_notes`` is filled when a post-create failure triggered
    orphan cleanup, so the error surface can say what happened to the
    half-created item either way.

    ``export`` returns the path of a GeoPackage it has just written. It
    is a callable rather than a path because it has to run HERE, on the
    worker: writing a large layer takes seconds to minutes, and doing
    it on the GUI thread froze the window between pressing Publish and
    the first sign of progress.

    ``delete_gpkg`` removes that export once staging has finished with
    it, success or failure: the portal keeps its own copy under
    ``/tmp/gg-staging/<id>/`` and the local tempfile has no further
    use. Callers handing over a file they did not create should pass
    False.
    """
    client = get_client(profile)

    _raise_if_canceled(handle)
    gpkg_path = export()
    handle.set_progress(PCT_EXPORTED)

    try:
        _raise_if_canceled(handle)
        staged = client.ingest.stage(file_path=gpkg_path)
    finally:
        # After staging either way, and after a cancel that lands
        # between the two. The portal has its copy on success, and on
        # failure there is nothing to keep.
        if delete_gpkg:
            _safe_unlink(gpkg_path)
    handle.set_progress(PCT_STAGED)
    _raise_if_canceled(handle)

    if not staged.layers:
        raise NoLayersProbed(
            "The portal read the uploaded file and found no layers in it. "
            "Check the layer's geometry validity in QGIS and try again."
        )

    # Every probed layer becomes a v3 layer on ONE item. The single-
    # layer publish is the one-element case of the same shape, which
    # is exactly how the portal's own multi-layer items (v3) model it.
    # Order is the GeoPackage's layer order, which the exporters write
    # as the QGIS stacking order.
    probes = list(staged.layers)
    v3_layers = [
        layer_from_probe(probe_layer=probe.to_api_dict()) for probe in probes
    ]
    envelope = build_data_layer_envelope(layers=v3_layers)

    item = client.items.create(
        type="data_layer",
        title=title,
        description=description,
        data=envelope,
        access=access,
    )
    handle.set_progress(PCT_ITEM_CREATED)

    # No transaction spans the create and the enqueues, so any enqueue
    # failure cleans up the item rather than stranding it half-filled.
    # Cancellation is checked INSIDE the guard rather than before the
    # create, so a cancel that lands in this window cleans up like any
    # other failure instead of leaving the orphan it was trying to
    # avoid. With several layers, a failure partway leaves earlier
    # jobs running against an item being deleted; the portal treats a
    # job against a deleted item as a no-op, so the cleanup still wins.
    jobs: list[Any] = []
    try:
        for probe, v3_layer in zip(probes, v3_layers, strict=True):
            _raise_if_canceled(handle)
            jobs.append(
                client.import_jobs.enqueue(
                    item_id=item.id,
                    layer_id=v3_layer.id,
                    staging_id=staged.staging_id,
                    source_layer_name=probe.name,
                    mode="replace",
                )
            )
    except BaseException:
        if _delete_item_quietly(client, item.id):
            cleanup_notes.append("The partly created portal item was removed.")
        else:
            cleanup_notes.append(
                f"A partly created portal item ({item.id}) could not be "
                "removed; delete it in the portal if it appears."
            )
        raise

    handle.set_progress(PCT_ENQUEUED)
    return VectorPublishOutcome(
        item_id=item.id,
        layer_id=v3_layers[0].id,
        job=jobs[0],
        layer_ids=tuple(layer.id for layer in v3_layers),
        jobs=tuple(jobs),
    )


def _delete_item_quietly(client: GratisGISClient, item_id: str) -> bool:
    """Best-effort delete for orphan cleanup; never raises."""
    try:
        client.items.delete(item_id)
    except Exception:
        _log.exception("cleanup delete of item %s failed", item_id)
        return False
    return True


def _raise_if_canceled(handle: TaskHandle) -> None:
    if handle.is_canceled():
        raise TaskCancelledError("Publish cancelled")


def _safe_unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
