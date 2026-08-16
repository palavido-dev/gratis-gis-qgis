# SPDX-License-Identifier: AGPL-3.0-or-later
"""The decisions behind the Processing algorithms.

The algorithm classes only construct under real bindings and are
exercised by the smoke test; what lives here is everything they
decide: which connection a run uses, how a feedback maps onto the
pipeline's handle, and when a job poll gives up.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gratisgis_qgis.processing.support import (
    ACCESS_CHOICES,
    FeedbackHandle,
    resolve_connection,
    wait_for_import_job,
)
from tests.plugin.conftest import ProfileFactory


class _Store:
    def __init__(self, profiles: dict[str, Any]) -> None:
        self._profiles = profiles

    def list_names(self) -> list[str]:
        return sorted(self._profiles)

    def get(self, name: str) -> Any:
        return self._profiles.get(name)


class TestResolveConnection:
    def test_a_named_signed_in_connection_wins(
        self, profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory(name="demo", authcfg_id="auth-1")
        store = _Store({"demo": profile})
        assert resolve_connection(store, "demo") is profile

    def test_blank_resolves_the_single_signed_in_one(
        self, profile_factory: ProfileFactory
    ) -> None:
        signed = profile_factory(name="demo", authcfg_id="auth-1")
        out = _Store(
            {"demo": signed, "other": profile_factory(name="other", authcfg_id="")}
        )
        assert resolve_connection(out, "") is signed

    def test_blank_with_two_signed_in_is_an_error_naming_both(
        self, profile_factory: ProfileFactory
    ) -> None:
        """Guessing would publish to whichever sorted first, the wrong
        surprise for a batch tool."""
        store = _Store({
            "a": profile_factory(name="a", authcfg_id="x"),
            "b": profile_factory(name="b", authcfg_id="y"),
        })
        result = resolve_connection(store, "")
        assert isinstance(result, str)
        assert "a" in result and "b" in result

    def test_an_unknown_name_lists_what_exists(
        self, profile_factory: ProfileFactory
    ) -> None:
        store = _Store({"demo": profile_factory(name="demo")})
        result = resolve_connection(store, "prod")
        assert isinstance(result, str)
        assert "demo" in result

    def test_a_signed_out_named_connection_says_so(
        self, profile_factory: ProfileFactory
    ) -> None:
        store = _Store({"demo": profile_factory(name="demo", authcfg_id="")})
        result = resolve_connection(store, "demo")
        assert isinstance(result, str)
        assert "not signed in" in result

    def test_nothing_signed_in_points_at_the_dialog(self) -> None:
        result = resolve_connection(_Store({}), "")
        assert isinstance(result, str)
        assert "Manage GratisGIS connections" in result

    def test_access_choices_match_the_portal_vocabulary(self) -> None:
        assert [v for v, _l in ACCESS_CHOICES] == ["private", "org", "public"]


class TestFeedbackHandle:
    def test_it_speaks_the_pipeline_protocol(self) -> None:
        feedback = SimpleNamespace(
            isCanceled=lambda: True, setProgress=lambda _p: None
        )
        handle = FeedbackHandle(feedback)
        assert handle.is_canceled() is True
        handle.set_progress(50)  # must not raise

    def test_a_broken_feedback_never_breaks_the_run(self) -> None:
        """Feedback objects die when a dialog closes mid-run; the
        pipeline must keep its own outcome."""
        def boom() -> bool:
            raise RuntimeError("gone")

        handle = FeedbackHandle(SimpleNamespace(isCanceled=boom))
        assert handle.is_canceled() is False
        handle.set_progress(10)  # no setProgress at all; still fine


class _Job:
    def __init__(self, status: str, percent: float | None = None) -> None:
        self.status = status
        self.percent_complete = percent

    @property
    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")


class _JobsClient:
    def __init__(self, sequence: list[_Job]) -> None:
        self._sequence = sequence
        self.calls = 0
        self.import_jobs = self

    def get(self, _job_id: str) -> _Job:
        job = self._sequence[min(self.calls, len(self._sequence) - 1)]
        self.calls += 1
        return job


class _Handle:
    def __init__(self, cancel_after: int | None = None) -> None:
        self.progress: list[float] = []
        self._polls = 0
        self._cancel_after = cancel_after

    def is_canceled(self) -> bool:
        self._polls += 1
        return self._cancel_after is not None and self._polls > self._cancel_after

    def set_progress(self, percent: float) -> None:
        self.progress.append(percent)


class TestWaitForImportJob:
    def _wait(self, client: _JobsClient, handle: _Handle, **kw: Any) -> Any:
        ticks = iter(range(1000))
        return wait_for_import_job(
            client, "job-1", handle,
            sleep=lambda _s: None, clock=lambda: float(next(ticks)),
            **kw,
        )

    def test_a_succeeding_job_returns_when_done(self) -> None:
        client = _JobsClient(
            [_Job("running", 40.0), _Job("running", 80.0), _Job("succeeded")]
        )
        handle = _Handle()
        job = self._wait(client, handle)
        assert job.status == "succeeded"
        assert client.calls == 3
        assert handle.progress, "progress reached the feedback"

    def test_a_failed_job_raises_with_its_status(self) -> None:
        client = _JobsClient([_Job("failed")])
        with pytest.raises(RuntimeError, match="failed"):
            self._wait(client, _Handle())

    def test_cancel_stops_the_poll(self) -> None:
        client = _JobsClient([_Job("running")] * 50)
        with pytest.raises(RuntimeError, match="Cancelled"):
            self._wait(client, _Handle(cancel_after=2))

    def test_a_stuck_job_times_out_with_directions(self) -> None:
        """A wedged worker must not pin a batch run forever, and the
        message says where to look afterwards."""
        client = _JobsClient([_Job("running")] * 1000)
        with pytest.raises(RuntimeError, match="portal"):
            self._wait(client, _Handle(), timeout_s=5.0)
