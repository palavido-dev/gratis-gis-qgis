# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish-raster dialog (Phase 5).

Uploads a PMTiles / MBTiles / GeoTIFF / COG / JP2 file as a portal
tile_layer item. The portal does any server-side conversion
(MBTiles -> PMTiles, raw raster -> COG -> PMTiles).

We deliberately don't try to convert raster -> PMTiles in the
plugin. That would require an external CLI binary (tippecanoe /
pmtiles) which we can't reliably bundle in a QGIS plugin. Users
who want PMTiles directly should convert externally and upload
the .pmtiles file; everyone else uploads raw GeoTIFF and lets the
portal do the work.

The publish flow, all inside ONE background task so a multi-GB
upload never blocks the GUI thread:

  1. Create an empty tile_layer item via items.create.
  2. Best-effort disk-space check (fail-open).
  3. Presign the upload, declaring the file size so the portal can
     enforce its size cap before any bytes move.
  4. PUT the file to MinIO, streamed from disk in chunks (constant
     memory) with live progress and cancel.
  5. Finalize so the portal reads the header.

The PUT goes through the client's transport seam rather than any
HTTP library of its own, with the connection profile's TLS-verify
setting, so portals on self-signed certificates behave the same
here as on every other call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from gratisgis_client.transport import TransportRequest, UrllibTransport

from ..log import get_logger
from ..portal import get_client
from ..tasks import (
    TaskCancelledError,
    TaskHandle,
)

if TYPE_CHECKING:
    pass  # type: ignore[import-not-found]

_log = get_logger(__name__)

# 30-minute socket timeout on the PUT because a multi-GB GeoTIFF on
# a slow link genuinely takes a while; the connect itself still
# fails fast via the transport's normal error path.
_UPLOAD_TIMEOUT = 1800.0

# Progress budget: the four metadata calls bracket the upload, which
# gets the wide middle band because it is where the wall-clock goes.
_PCT_ITEM_CREATED = 2.0
_PCT_UPLOAD_START = 6.0
_PCT_UPLOAD_END = 96.0


@dataclass(frozen=True)
class _PublishOutcome:
    """What the pipeline hands back to the GUI callback."""

    item_id: str
    needs_server_conversion: bool


def run_raster_pipeline(
    handle: TaskHandle,
    *,
    profile,
    file_path: str,
    file_name: str,
    size: int,
    title: str,
    description: str | None,
    access: str,
    needs_server_conversion: bool,
    cleanup_notes: list[str],
) -> _PublishOutcome:
    """Create + check-space + presign + upload + finalize, in order.

    Raises on any hard failure; the dialog's error callback renders
    it. Any exit after the item create that is not a successful
    finalize (failure or cancel alike) would strand an empty
    tile_layer item, so the created item is deleted best-effort and
    the outcome recorded in ``cleanup_notes`` for the dialog's error
    surface. The original exception always propagates unchanged so
    cancels still present as cancels.
    """
    client = get_client(profile)

    item = client.items.create(
        type="tile_layer",
        title=title,
        description=description,
        data={"version": 1, "processingState": "uploading"},
        access=access,
    )
    try:
        handle.set_progress(_PCT_ITEM_CREATED)
        _raise_if_canceled(handle)

        # Best-effort disk-space check: a portal-side refusal is a
        # hard stop with its reason, but an errored check falls open
        # and lets the real PUT surface any genuine shortage.
        try:
            space = client.storage.check_tile_layer_space(
                file_name=file_name, size_bytes=size
            )
        except Exception:
            _log.exception("check-space failed (fail-open)")
        else:
            if not space.ok:
                raise RuntimeError(space.reason or "Portal reports insufficient disk.")
        _raise_if_canceled(handle)

        # Declaring the size lets the portal refuse oversized files at
        # presign time and bakes Content-Length into the signature; the
        # PUT below must therefore send exactly this length.
        presigned = client.storage.presign_upload(
            kind="item-tile-layer",
            content_type="application/octet-stream",
            size_bytes=size,
        )
        if presigned.max_bytes and size > presigned.max_bytes:
            raise RuntimeError(
                f"File is {size / 1024 / 1024:.1f} MB; portal allows "
                f"{presigned.max_bytes / 1024 / 1024 / 1024:.1f} GB per file."
            )
        handle.set_progress(_PCT_UPLOAD_START)
        _raise_if_canceled(handle)

        _upload_to_presigned(
            presigned.upload_url,
            file_path,
            size=size,
            verify_tls=profile.verify_tls,
            handle=handle,
        )
        handle.set_progress(_PCT_UPLOAD_END)
        _raise_if_canceled(handle)

        storage_key = f"item-tile-layer/{presigned.key}"
        client.tile_layer.finalize(
            item_id=item.id,
            storage_key=storage_key,
            storage_url=presigned.public_url,
            file_name=file_name,
            size_bytes=size,
        )
    except BaseException:
        if _delete_item_quietly(client, item.id):
            cleanup_notes.append(
                "The partly created tile layer item was removed from the portal."
            )
        else:
            cleanup_notes.append(
                f"A partly created tile layer item ({item.id}) could not be "
                "removed; delete it in the portal if it appears."
            )
        raise
    handle.set_progress(100.0)
    return _PublishOutcome(
        item_id=item.id, needs_server_conversion=needs_server_conversion
    )


def _delete_item_quietly(client, item_id: str) -> bool:
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


class _ProgressFileReader:
    """File wrapper that reports upload progress and honors cancel.

    urllib drains the request body through ``read(n)``, so metering
    here observes exactly what went onto the socket buffer. Raising
    from ``read`` is also the only reliable way to abort an in-flight
    urllib upload; the transport surfaces it to the task as-is.
    """

    def __init__(self, fh: IO[bytes], total: int, handle: TaskHandle) -> None:
        self._fh = fh
        self._total = max(1, total)
        self._sent = 0
        self._handle = handle

    def read(self, n: int = -1) -> bytes:
        if self._handle.is_canceled():
            raise TaskCancelledError("Upload cancelled")
        chunk = self._fh.read(n)
        self._sent += len(chunk)
        span = _PCT_UPLOAD_END - _PCT_UPLOAD_START
        self._handle.set_progress(
            _PCT_UPLOAD_START + span * min(1.0, self._sent / self._total)
        )
        return chunk


def _upload_to_presigned(
    upload_url: str,
    file_path: str,
    *,
    size: int,
    verify_tls: bool,
    handle: TaskHandle,
) -> None:
    """PUT the file bytes to MinIO via the presigned URL.

    Streams from disk in chunks (constant memory however large the
    raster) through the client's transport seam. The explicit
    Content-Length is load-bearing twice over: without it urllib
    switches to chunked transfer encoding, which S3-style endpoints
    reject, and the presigned signature covers the declared length.
    ``verify_tls`` carries the connection profile's setting so
    portals on self-signed certificates work here too.
    """
    transport = UrllibTransport(verify_tls=verify_tls)
    with open(file_path, "rb") as fh:
        response = transport.send(
            TransportRequest(
                method="PUT",
                url=upload_url,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                },
                body=_ProgressFileReader(fh, size, handle),
                timeout=_UPLOAD_TIMEOUT,
            )
        )
    if response.status >= 300:
        raise RuntimeError(
            f"PUT to storage failed: HTTP {response.status} {response.text[:200]}"
        )
