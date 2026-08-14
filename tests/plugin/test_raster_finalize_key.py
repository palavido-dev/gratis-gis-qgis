# SPDX-License-Identifier: AGPL-3.0-or-later
"""The storage key the raster publish hands to finalize.

The portal's presign response carries the COMPLETE key, prefix and
all. The publish pipeline composed one anyway, producing
``item-tile-layer/item-tile-layer/<uuid>``, which names no object; the
portal then failed reading the upload back and answered 400.

It stayed hidden because nothing asserted the key, and because the
portal used to be handed a URL as well and dereferenced that instead.
Once it started streaming by key (closing an SSRF hole), the wrong key
became fatal.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.plugin.conftest import install_qgis_stub

_KEY = "item-tile-layer/db4482f8-e8c4-42ed-980b-94b70cf5b5eb"


@pytest.fixture
def raster_mod(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    install_qgis_stub(
        monkeypatch,
        {
            "qgis.core": {},
            "qgis.PyQt.QtCore": {"QSettings": type("QSettings", (), {})},
            "qgis.PyQt.QtWidgets": {},
        },
    )
    import gratisgis_qgis.ui.publish_raster_dialog as mod

    return mod


class _Recorder:
    """A client that records the finalize call and does nothing else."""

    def __init__(self) -> None:
        self.finalize_kwargs: dict[str, Any] = {}
        outer = self

        class _Items:
            @staticmethod
            def create(**_kwargs: Any) -> Any:
                return SimpleNamespace(id="item-1")

            @staticmethod
            def delete(_item_id: str) -> None:
                return None

        class _Storage:
            @staticmethod
            def check_tile_layer_space(**_kwargs: Any) -> Any:
                return SimpleNamespace(ok=True, reason=None)

            @staticmethod
            def presign_upload(**_kwargs: Any) -> Any:
                return SimpleNamespace(
                    upload_url="https://minio.example/put",
                    public_url="/api/portal/storage/private/" + _KEY,
                    key=_KEY,
                    max_bytes=0,
                )

        class _TileLayer:
            @staticmethod
            def finalize(**kwargs: Any) -> dict[str, Any]:
                outer.finalize_kwargs = kwargs
                return {}

        self.items = _Items()
        self.storage = _Storage()
        self.tile_layer = _TileLayer()


def _run(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    client = _Recorder()
    monkeypatch.setattr(mod, "get_client", lambda _profile: client)
    monkeypatch.setattr(mod, "_upload_to_presigned", lambda *a, **k: None)
    handle = SimpleNamespace(
        set_progress=lambda _pct: None, is_canceled=lambda: False
    )
    mod.run_raster_pipeline(
        handle,
        profile=SimpleNamespace(verify_tls=True),
        file_path="/tmp/aerial.tif",
        file_name="aerial.tif",
        size=1234,
        title="Aerial",
        description=None,
        access="private",
        needs_server_conversion=True,
        cleanup_notes=[],
    )
    return client


def test_the_presigned_key_is_passed_through_unchanged(
    raster_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(raster_mod, monkeypatch)
    assert client.finalize_kwargs["storage_key"] == _KEY


def test_the_prefix_is_not_added_a_second_time(
    raster_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exact shape of the bug, named so a reintroduction is obvious
    # from the failure rather than needing the portal to explain it.
    client = _run(raster_mod, monkeypatch)
    key = client.finalize_kwargs["storage_key"]
    assert not key.startswith("item-tile-layer/item-tile-layer/")
    assert key.count("item-tile-layer/") == 1


def test_the_rest_of_the_finalize_call_is_what_the_portal_requires(
    raster_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every one of these is validated server-side, and an empty or
    # missing value is its own 400.
    client = _run(raster_mod, monkeypatch)
    kwargs = client.finalize_kwargs
    assert kwargs["item_id"] == "item-1"
    assert kwargs["file_name"] == "aerial.tif"
    assert kwargs["size_bytes"] == 1234
    assert kwargs["storage_url"]
