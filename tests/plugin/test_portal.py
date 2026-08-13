# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the shared per-profile client (`gratisgis_qgis.portal`).

Cache identity and invalidation are the load-bearing behaviors: a
rebuilt-per-call client would race token refreshes across threads,
and a cache that survives sign-out would keep serving dead tokens.
Network is never touched; the client class is swapped for a fake at
the module seam.
"""
from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import gratisgis_qgis.portal as portal
from tests.plugin.conftest import ProfileFactory


class _FakeClient:
    """Stands in for GratisGISClient at the portal-module seam."""

    def __init__(self, config: Any, token_storage: Any = None) -> None:
        self.config = config
        self.token_storage = token_storage
        self.closed = False
        self.items = SimpleNamespace(
            list=self._list,
            get=self._get,
        )
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.list_result: list[Any] = []
        self.get_result: Any = None
        self.get_raises: BaseException | None = None

    def close(self) -> None:
        self.closed = True

    def _list(self, **kwargs: Any) -> SimpleNamespace:
        self.list_calls.append(kwargs)
        return SimpleNamespace(items=self.list_result)

    def _get(self, item_id: str) -> Any:
        self.get_calls.append(item_id)
        if self.get_raises is not None:
            raise self.get_raises
        return self.get_result


def _client_for(profile: Any) -> _FakeClient:
    """get_client narrowed to the fake so tests can poke its guts."""
    client = portal.get_client(profile)
    assert isinstance(client, _FakeClient)
    return client


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    # Cache is module-global state; every test starts and ends empty
    # so identity assertions cannot leak across tests.
    with portal._lock:
        portal._clients.clear()
    yield
    with portal._lock:
        portal._clients.clear()


@pytest.fixture
def fake_client_cls(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    monkeypatch.setattr(portal, "GratisGISClient", _FakeClient)
    return _FakeClient


class TestGetClientCache:
    def test_same_profile_returns_same_client(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory()
        first = portal.get_client(profile)
        second = portal.get_client(profile)
        assert first is second

    def test_equal_profile_value_hits_the_same_entry(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        # Profiles are frozen dataclasses re-read from QSettings all
        # the time; two distinct instances describing the same
        # connection must share one client.
        first = portal.get_client(profile_factory())
        second = portal.get_client(profile_factory())
        assert first is second

    def test_different_authcfg_gets_a_different_client(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        a = portal.get_client(profile_factory(authcfg_id="aaaaaaa"))
        b = portal.get_client(profile_factory(authcfg_id="bbbbbbb"))
        assert a is not b

    def test_different_portal_url_gets_a_different_client(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        a = portal.get_client(profile_factory())
        b = portal.get_client(
            profile_factory(
                portal_url="https://other.example",
                api_base_url="https://other.example/api",
                oidc_issuer="https://other.example/realms/gratis-gis",
            )
        )
        assert a is not b

    def test_undiscovered_profile_raises(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        bare = profile_factory(
            portal_name="", portal_version="", api_base_url="", oidc_issuer="", discovered_at=0.0
        )
        with pytest.raises(ValueError):
            portal.get_client(bare)


class TestInvalidate:
    def test_invalidate_profile_closes_and_rebuilds(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory()
        first = _client_for(profile)
        portal.invalidate(profile)
        assert first.closed is True
        second = _client_for(profile)
        assert second is not first

    def test_invalidate_by_authcfg_id_string(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory(authcfg_id="zzzzzzz")
        first = _client_for(profile)
        portal.invalidate("zzzzzzz")
        assert first.closed is True
        assert _client_for(profile) is not first

    def test_invalidate_leaves_other_profiles_alone(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        keep = _client_for(profile_factory(authcfg_id="keep111"))
        drop = _client_for(profile_factory(authcfg_id="drop222"))
        portal.invalidate("drop222")
        assert drop.closed is True
        assert keep.closed is False
        assert _client_for(profile_factory(authcfg_id="keep111")) is keep

    def test_invalidate_unknown_id_is_a_noop(
        self, fake_client_cls: type[_FakeClient]
    ) -> None:
        portal.invalidate("nothing")


class TestListItems:
    def test_not_signed_in_returns_empty_without_building_a_client(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory(authcfg_id="")
        assert portal.list_items(profile) == []
        assert not portal._clients

    def test_undiscovered_returns_empty(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory(
            portal_name="", portal_version="", api_base_url="", oidc_issuer="", discovered_at=0.0
        )
        assert portal.list_items(profile) == []

    def test_passes_filters_through_and_returns_items(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory()
        client = _client_for(profile)
        sentinel = object()
        client.list_result = [sentinel]
        out = portal.list_items(
            profile, types=["map"], query="parcels", owner_id="u1", limit=25
        )
        assert out == [sentinel]
        assert client.list_calls == [
            {"types": ["map"], "query": "parcels", "owner_id": "u1", "limit": 25}
        ]

    def test_default_limit_matches_old_fetch_module(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory()
        client = _client_for(profile)
        portal.list_items(profile)
        assert client.list_calls[0]["limit"] == 200


class TestGetItem:
    def test_not_signed_in_returns_none(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        assert portal.get_item(profile_factory(authcfg_id=""), "item-1") is None

    def test_returns_wire_dict_from_item(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory()
        client = _client_for(profile)
        client.get_result = SimpleNamespace(to_api_dict=lambda: {"id": "item-1", "type": "map"})
        assert portal.get_item(profile, "item-1") == {"id": "item-1", "type": "map"}
        assert client.get_calls == ["item-1"]

    def test_swallows_errors_to_none(
        self, fake_client_cls: type[_FakeClient], profile_factory: ProfileFactory
    ) -> None:
        profile = profile_factory()
        client = _client_for(profile)
        client.get_raises = RuntimeError("portal down")
        assert portal.get_item(profile, "item-1") is None
