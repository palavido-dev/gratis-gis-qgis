# SPDX-License-Identifier: AGPL-3.0-or-later
"""Qt-free helpers behind the Processing algorithms.

Kept apart from the algorithm classes because those inherit from
QgsProcessingAlgorithm and only construct under real bindings; the
decisions (which connection an algorithm uses, how feedback maps to
the pipeline's handle, when a job poll should stop) are testable
without any of that.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any

#: Access choices in the order the enum parameter shows them. The
#: portal vocabulary, with the same plain labels the sharing dialog
#: uses.
ACCESS_CHOICES: tuple[tuple[str, str], ...] = (
    ("private", "Only me"),
    ("org", "My organization"),
    ("public", "Everyone"),
)


def resolve_connection(store: Any, requested: str) -> Any:
    """The profile an algorithm should use, or a str error.

    An empty request means "the obvious one": the single signed-in
    connection. With several signed in, guessing would publish to
    whichever sorted first, which is exactly the wrong surprise for a
    batch tool, so ambiguity is an error naming the candidates.
    """
    requested = requested.strip()
    if requested:
        profile = store.get(requested)
        if profile is None:
            names = ", ".join(store.list_names()) or "none configured"
            return (
                f"No connection named {requested!r}. "
                f"Configured connections: {names}."
            )
        if not profile.authcfg_id:
            return f"Connection {requested!r} is not signed in."
        return profile

    signed_in = [
        p
        for name in store.list_names()
        if (p := store.get(name)) is not None and p.authcfg_id
    ]
    if not signed_in:
        return (
            "No signed-in GratisGIS connection. Sign in first via "
            "Manage GratisGIS connections."
        )
    if len(signed_in) > 1:
        names = ", ".join(p.name for p in signed_in)
        return (
            "More than one connection is signed in; set the Connection "
            f"parameter to one of: {names}."
        )
    return signed_in[0]


class FeedbackHandle:
    """The pipeline's TaskHandle protocol over a Processing feedback.

    The pipelines poll ``is_canceled`` and push ``set_progress``; a
    Processing run polls feedback.isCanceled() and shows
    setProgress(). Same shape, different spelling.
    """

    def __init__(self, feedback: Any) -> None:
        self._feedback = feedback

    def is_canceled(self) -> bool:
        try:
            return bool(self._feedback.isCanceled())
        except Exception:
            return False

    def set_progress(self, percent: float) -> None:
        with contextlib.suppress(Exception):
            self._feedback.setProgress(float(percent))


def wait_for_import_job(
    client: Any,
    job_id: str,
    handle: Any,
    *,
    timeout_s: float = 600.0,
    poll_s: float = 2.0,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
) -> Any:
    """Poll an import job to its end, honouring cancel.

    A batch run needs the finished item, not an enqueued job: the next
    model step usually consumes what this one published. Raises on
    failure, cancel, and timeout; the timeout exists because a stuck
    worker would otherwise pin a batch run forever, and ten minutes is
    past any import the demo portal has ever run.
    """
    deadline = clock() + timeout_s
    while True:
        job = client.import_jobs.get(job_id)
        if job.is_terminal:
            if job.status != "succeeded":
                raise RuntimeError(
                    f"The portal import did not finish (status: {job.status})."
                )
            return job
        if handle.is_canceled():
            raise RuntimeError("Cancelled.")
        if clock() > deadline:
            raise RuntimeError(
                "The portal import is still running after "
                f"{int(timeout_s)} seconds. Check the item on the portal; "
                "the import may yet finish."
            )
        percent = job.percent_complete
        if percent is not None:
            # The pipeline used 0-90 for upload and probe; the import
            # walks the last stretch.
            handle.set_progress(90.0 + percent / 10.0)
        sleep(poll_s)
