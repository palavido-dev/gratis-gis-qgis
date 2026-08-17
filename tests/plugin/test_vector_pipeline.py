# SPDX-License-Identifier: AGPL-3.0-or-later
"""The whole vector publish, from staged file to enqueued job.

This sequence used to be a chain of nested callbacks inside
``PublishLayerDialog``: a task to stage, a GUI hop to probe and build
the envelope, another task to create and enqueue. Only the last of
those was reachable from a test, so the probe handling and the
envelope construction, which are where the portal's answer gets turned
into what we send back, had no coverage at all.

Note that no Qt stub appears anywhere below. That is the point of the
extraction rather than a side effect: the pipeline imports no widgets,
so the tests do not have to pretend to be a dialog to reach it.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gratisgis_qgis.publish.vector_pipeline import (
    NoLayersProbed,
    VectorPublishOutcome,
    run_vector_pipeline,
)
from gratisgis_qgis.tasks import InlineTaskHandle, TaskCancelledError


class _Probe:
    """One layer as the portal's stage response describes it."""

    def __init__(self, name: str = "parcels", **overrides: Any) -> None:
        self.name = name
        self._dict = {
            "name": name,
            "geometryType": "polygon",
            "fields": [{"name": "owner", "type": "string"}],
            "featureCount": 3,
        }
        self._dict.update(overrides)

    def to_api_dict(self) -> dict[str, Any]:
        return dict(self._dict)


class _FakeItems:
    def __init__(self, *, delete_raises: Exception | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._delete_raises = delete_raises

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(id="item-1")

    def delete(self, item_id: str) -> None:
        if self._delete_raises is not None:
            raise self._delete_raises
        self.deleted.append(item_id)


class _FakeClient:
    def __init__(
        self,
        *,
        layers: list[_Probe] | None = None,
        stage_raises: Exception | None = None,
        enqueue_raises: Exception | None = None,
        delete_raises: Exception | None = None,
    ) -> None:
        self.items = _FakeItems(delete_raises=delete_raises)
        self.staged_paths: list[str] = []
        self.enqueued: list[dict[str, Any]] = []
        self._layers = [_Probe()] if layers is None else layers
        self._stage_raises = stage_raises
        self._enqueue_raises = enqueue_raises
        self.job = SimpleNamespace(id="job-1")

        outer = self

        class _Ingest:
            def stage(self, *, file_path: str) -> SimpleNamespace:
                outer.staged_paths.append(file_path)
                if outer._stage_raises is not None:
                    raise outer._stage_raises
                return SimpleNamespace(
                    staging_id="stage-1", layers=list(outer._layers)
                )

        class _Jobs:
            def enqueue(self, **kwargs: Any) -> SimpleNamespace:
                outer.enqueued.append(kwargs)
                if outer._enqueue_raises is not None:
                    raise outer._enqueue_raises
                return outer.job

        self.ingest = _Ingest()
        self.import_jobs = _Jobs()


@pytest.fixture
def gpkg(tmp_path: Path) -> str:
    path = tmp_path / "export.gpkg"
    path.write_bytes(b"not really a geopackage")
    return str(path)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
    gpkg_path: str,
    *,
    handle: InlineTaskHandle | None = None,
    notes: list[str] | None = None,
    **overrides: Any,
) -> VectorPublishOutcome:
    import gratisgis_qgis.publish.vector_pipeline as mod

    monkeypatch.setattr(mod, "get_client", lambda _p: client)
    kwargs: dict[str, Any] = {
        "profile": SimpleNamespace(name="demo"),
        "export": lambda: gpkg_path,
        "title": "Parcels",
        "description": None,
        "access": "private",
        "cleanup_notes": notes if notes is not None else [],
    }
    kwargs.update(overrides)
    return run_vector_pipeline(handle or InlineTaskHandle(), **kwargs)


class TestHappyPath:
    def test_it_stages_creates_and_enqueues(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        client = _FakeClient()
        outcome = _run(monkeypatch, client, gpkg)

        assert client.staged_paths == [gpkg]
        assert outcome.item_id == "item-1"
        assert outcome.job is client.job
        [created] = client.items.created
        assert created["type"] == "data_layer"
        assert created["title"] == "Parcels"
        assert created["access"] == "private"

    def test_the_envelope_carries_what_the_portal_probed(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The probe response is the only description of the data we get.

        Building the envelope from it was the step buried deepest in
        the old callback chain, and a wrong geometry type or a dropped
        field here produces an item whose schema does not match the
        rows about to be imported into it.
        """
        client = _FakeClient(
            layers=[
                _Probe(
                    "roads",
                    geometryType="line",
                    fields=[
                        {"name": "surface", "type": "string"},
                        {"name": "lanes", "type": "number"},
                    ],
                )
            ]
        )
        _run(monkeypatch, client, gpkg)

        envelope = client.items.created[0]["data"]
        [layer] = envelope["layers"]
        assert layer["geometryType"] == "line"
        assert [f["name"] for f in layer["fields"]] == ["surface", "lanes"]

    def test_the_enqueue_names_the_probed_source_layer(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The portal looks the layer up inside the staged file by name.

        Sending our own layer id here instead of the probed name is a
        plausible mistake that imports nothing while reporting success.
        """
        client = _FakeClient(layers=[_Probe("Parcels 2024")])
        outcome = _run(monkeypatch, client, gpkg)

        [enqueued] = client.enqueued
        assert enqueued["source_layer_name"] == "Parcels 2024"
        assert enqueued["staging_id"] == "stage-1"
        assert enqueued["item_id"] == "item-1"
        assert enqueued["layer_id"] == outcome.layer_id
        assert enqueued["mode"] == "replace"

    def test_the_returned_layer_id_matches_the_envelope(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The dialog polls with this id; a mismatch polls nothing."""
        client = _FakeClient(layers=[_Probe("Parcels 2024")])
        outcome = _run(monkeypatch, client, gpkg)
        [layer] = client.items.created[0]["data"]["layers"]
        assert outcome.layer_id == layer["id"]


class TestTheExportRunsOnTheWorker:
    """Writing the GeoPackage is part of the job, not a prelude to it.

    It used to run on the GUI thread before the task was scheduled, so
    a large layer froze the QGIS window between pressing Publish and
    the first sign of anything happening: the worst possible moment to
    look hung, because the user cannot tell it from a crash.
    """

    def test_the_export_is_called_by_the_pipeline(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        calls: list[str] = []

        def export() -> str:
            calls.append("exported")
            return gpkg

        _run(monkeypatch, _FakeClient(), gpkg, export=export)
        assert calls == ["exported"]

    def test_nothing_is_staged_before_the_export_returns(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """Order, pinned: the file has to exist before it is uploaded."""
        order: list[str] = []
        client = _FakeClient()
        real_stage = client.ingest.stage

        def stage(*, file_path: str) -> Any:
            order.append("staged")
            return real_stage(file_path=file_path)

        client.ingest.stage = stage  # type: ignore[method-assign]

        def export() -> str:
            order.append("exported")
            return gpkg

        _run(monkeypatch, client, gpkg, export=export)
        assert order == ["exported", "staged"]

    def test_an_export_failure_reaches_the_error_callback(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """It used to be caught inline and shown in its own message box.

        On the worker it is an ordinary task failure, so the message
        has to survive the trip rather than being swallowed.
        """
        def export() -> str:
            raise RuntimeError("GeoPackage write failed: disk full")

        with pytest.raises(RuntimeError, match="disk full"):
            _run(monkeypatch, _FakeClient(), gpkg, export=export)

    def test_a_failed_export_uploads_nothing(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        client = _FakeClient()

        def export() -> str:
            raise RuntimeError("no")

        with pytest.raises(RuntimeError):
            _run(monkeypatch, client, gpkg, export=export)
        assert client.staged_paths == []
        assert client.items.created == []

    def test_a_cancel_before_the_export_does_not_write_the_file(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """Cancelling should stop the expensive part, not just skip it.

        Exporting a 1.4M-feature layer and then throwing it away is the
        whole cost of the operation for no result.
        """
        handle = InlineTaskHandle()
        handle.cancel()
        calls: list[str] = []

        def export() -> str:
            calls.append("exported")
            return gpkg

        with pytest.raises(TaskCancelledError):
            _run(
                monkeypatch, _FakeClient(), gpkg, handle=handle, export=export
            )
        assert calls == []


class TestTheLocalExport:
    def test_the_temp_file_is_deleted_after_a_successful_stage(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The portal has its own copy; ours is a whole layer on disk."""
        _run(monkeypatch, _FakeClient(), gpkg)
        assert not Path(gpkg).exists()

    def test_the_temp_file_is_deleted_when_staging_fails(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """Otherwise a failing portal quietly fills the user's disk.

        The failure path is the one that repeats: a user retrying a
        publish against a portal that is down leaves one export behind
        per attempt.
        """
        client = _FakeClient(stage_raises=RuntimeError("portal down"))
        with pytest.raises(RuntimeError):
            _run(monkeypatch, client, gpkg)
        assert not Path(gpkg).exists()

    def test_a_caller_that_owns_the_file_can_keep_it(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """Publishing several layers from one export must not delete it."""
        _run(monkeypatch, _FakeClient(), gpkg, delete_gpkg=False)
        assert Path(gpkg).exists()

    def test_an_already_missing_file_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cleanup runs in a finally; it must not mask the real error."""
        missing = str(tmp_path / "gone.gpkg")
        client = _FakeClient(stage_raises=RuntimeError("portal down"))
        with pytest.raises(RuntimeError, match="portal down"):
            _run(monkeypatch, client, missing)


class TestEmptyProbe:
    def test_no_probed_layers_raises_its_own_error(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """Distinct type, because this one is the user's to fix.

        Usually invalid geometry. Reporting it as a generic portal
        failure sends them looking at the network instead of the layer.
        """
        client = _FakeClient(layers=[])
        with pytest.raises(NoLayersProbed) as excinfo:
            _run(monkeypatch, client, gpkg)
        assert "geometry" in str(excinfo.value).lower()

    def test_nothing_is_created_when_the_probe_is_empty(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The guard has to come before the create, not after.

        After it, every empty export leaves an orphan item behind that
        the user then has to find and delete in the portal.
        """
        client = _FakeClient(layers=[])
        with pytest.raises(NoLayersProbed):
            _run(monkeypatch, client, gpkg)
        assert client.items.created == []
        assert client.items.deleted == []


class TestOrphanCleanup:
    def test_an_enqueue_failure_deletes_the_created_item(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """No transaction spans create and enqueue."""
        client = _FakeClient(enqueue_raises=RuntimeError("enqueue exploded"))
        notes: list[str] = []
        with pytest.raises(RuntimeError, match="enqueue exploded"):
            _run(monkeypatch, client, gpkg, notes=notes)
        assert client.items.deleted == ["item-1"]
        assert notes == ["The partly created portal item was removed."]

    def test_a_failed_cleanup_names_the_item_for_the_user(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """If we cannot remove it, say which one to remove by hand."""
        client = _FakeClient(
            enqueue_raises=RuntimeError("enqueue exploded"),
            delete_raises=RuntimeError("delete refused"),
        )
        notes: list[str] = []
        with pytest.raises(RuntimeError, match="enqueue exploded"):
            _run(monkeypatch, client, gpkg, notes=notes)
        assert len(notes) == 1
        assert "item-1" in notes[0]
        assert "could not be removed" in notes[0]

    def test_the_original_error_wins_over_the_cleanup_error(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The user needs to know why the publish failed.

        A cleanup failure raised in its place would report a symptom of
        the first failure as though it were the cause.
        """
        client = _FakeClient(
            enqueue_raises=RuntimeError("enqueue exploded"),
            delete_raises=RuntimeError("delete refused"),
        )
        with pytest.raises(RuntimeError) as excinfo:
            _run(monkeypatch, client, gpkg)
        assert "enqueue exploded" in str(excinfo.value)
        assert "delete refused" not in str(excinfo.value)

    def test_a_successful_publish_deletes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        client = _FakeClient()
        notes: list[str] = []
        _run(monkeypatch, client, gpkg, notes=notes)
        assert client.items.deleted == []
        assert notes == []


class TestCancellation:
    def test_a_cancel_before_the_item_exists_creates_nothing(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        handle = InlineTaskHandle()
        handle.cancel()
        client = _FakeClient()
        with pytest.raises(TaskCancelledError):
            _run(monkeypatch, client, gpkg, handle=handle)
        assert client.items.created == []

    def test_a_cancel_after_the_item_exists_still_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """A cancel in that window strands an item exactly like a failure.

        The check sits inside the cleanup guard rather than before the
        create for this reason. Put it one line earlier and a cancel
        landing here leaves behind the orphan the guard exists to
        prevent, while still presenting to the user as a clean cancel.
        """

        class _CancelOnCreate(_FakeItems):
            def create(self, **kwargs: Any) -> SimpleNamespace:
                handle.cancel()
                return super().create(**kwargs)

        handle = InlineTaskHandle()
        client = _FakeClient()
        client.items = _CancelOnCreate()
        notes: list[str] = []

        with pytest.raises(TaskCancelledError):
            _run(monkeypatch, client, gpkg, handle=handle, notes=notes)

        assert client.items.deleted == ["item-1"]
        assert notes and "removed" in notes[0]
        assert client.enqueued == [], "the job must not be queued"


class TestProgress:
    def test_progress_advances_across_the_run(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The dialog drives its label off these, so they must move.

        Staging cannot report its own progress, so these bands are the
        only signal the user gets that anything is happening between
        picking a layer and the import job starting.
        """
        seen: list[float] = []

        class _RecordingHandle(InlineTaskHandle):
            def set_progress(self, pct: float) -> None:
                seen.append(pct)

        _run(monkeypatch, _FakeClient(), gpkg, handle=_RecordingHandle())
        assert seen == sorted(seen), f"progress went backwards: {seen}"
        assert seen[-1] == 100.0


class TestMultiLayerPublish:
    """Several probed layers become one item with one layer each."""

    def test_every_probed_layer_lands_on_the_item(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        client = _FakeClient(
            layers=[_Probe("parcels"), _Probe("summary"), _Probe("roads")]
        )
        outcome = _run(monkeypatch, client, gpkg)
        [created] = client.items.created
        assert len(created["data"]["layers"]) == 3
        assert len(outcome.layer_ids) == 3
        assert len(outcome.jobs) == 3

    def test_one_import_job_per_layer_in_order(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """Each layer's rows load through its own job; a single job
        would fill the first layer and leave the rest empty."""
        client = _FakeClient(layers=[_Probe("parcels"), _Probe("summary")])
        outcome = _run(monkeypatch, client, gpkg)
        names = [row["source_layer_name"] for row in client.enqueued]
        assert names == ["parcels", "summary"]
        ids = [row["layer_id"] for row in client.enqueued]
        assert list(outcome.layer_ids) == ids

    def test_an_enqueue_failure_partway_still_cleans_up_the_item(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        client = _FakeClient(
            layers=[_Probe("parcels"), _Probe("summary")],
            enqueue_raises=RuntimeError("portal said no"),
        )
        notes: list[str] = []
        with pytest.raises(RuntimeError):
            _run(monkeypatch, client, gpkg, notes=notes)
        assert client.items.deleted == ["item-1"]
        assert notes

    def test_the_single_layer_outcome_keeps_its_old_shape(
        self, monkeypatch: pytest.MonkeyPatch, gpkg: str
    ) -> None:
        """The dialog polls outcome.job and reads outcome.layer_id;
        the multi-layer fields must extend, not replace."""
        client = _FakeClient()
        outcome = _run(monkeypatch, client, gpkg)
        assert outcome.job is client.job
        assert outcome.layer_id == outcome.layer_ids[0]
        assert len(outcome.layer_ids) == 1
