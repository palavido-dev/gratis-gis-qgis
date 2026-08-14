# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the items endpoint wrapper."""

from __future__ import annotations

import pytest

from gratisgis_client.config import PortalConfig
from gratisgis_client.endpoints.items import ItemsEndpoint
from gratisgis_client.http import PortalHttp
from tests.client.transport_stub import (
    FakeTransport,
    body_json,
    json_response,
    path_of,
    query_of,
)


def _config() -> PortalConfig:
    return PortalConfig(
        portal_url="https://portal.example.com",
        keycloak_url="https://auth.example.com",
    )


class _FakeAuth:
    """Stand-in for AuthManager that hands out a static token.

    The real manager talks to Keycloak; for endpoint tests we just
    need request_json to attach an Authorization header.
    """

    def access_token(self) -> str:
        return "fake-token"

    def force_refresh(self) -> str:  # pragma: no cover
        return "fake-token"


def _endpoint(transport: FakeTransport) -> ItemsEndpoint:
    return ItemsEndpoint(PortalHttp(_config(), _FakeAuth(), transport=transport))


def _summary_payload(id_: str = "abc") -> dict[str, object]:
    return {
        "id": id_,
        "type": "data_layer",
        "title": "Example layer",
        "summary": None,
        "description": None,
        "tags": ["sample"],
        "access": "org",
        "ownerId": "user-1",
        "ownerUsername": "alice",
        "orgId": "org-1",
        "folderId": None,
        "thumbnailUrl": None,
        "createdAt": "2026-05-19T00:00:00Z",
        "updatedAt": "2026-05-19T00:00:00Z",
    }


def _item_payload(id_: str = "abc") -> dict[str, object]:
    base = _summary_payload(id_)
    base["data"] = {"layers": [{"id": "l1", "name": "Stuff"}]}
    base["license"] = "CC-BY-4.0"
    base["thumbnailDesign"] = None
    return base


def test_list_parses_bare_array() -> None:
    transport = FakeTransport().add(
        json_response([_summary_payload("a"), _summary_payload("b")])
    )
    result = _endpoint(transport).list()

    assert path_of(transport.requests[0]) == "/api/items"
    assert len(result.items) == 2
    assert {i.id for i in result.items} == {"a", "b"}


def test_list_parses_paginated_envelope() -> None:
    transport = FakeTransport().add(
        json_response({"items": [_summary_payload("a")], "total": 1, "nextCursor": None})
    )
    result = _endpoint(transport).list()

    assert len(result.items) == 1
    assert result.total == 1
    assert result.next_cursor is None


def test_list_passes_filters_as_query_params() -> None:
    transport = FakeTransport().add(json_response([]))
    _endpoint(transport).list(
        types=["data_layer", "map"], access="org", limit=10, query="parcel"
    )

    seen = query_of(transport.requests[0])
    assert seen.get("type") == ["data_layer", "map"]
    assert seen.get("access") == ["org"]
    assert seen.get("limit") == ["10"]
    assert seen.get("q") == ["parcel"]


def test_list_parses_summary_fields_and_aliases() -> None:
    transport = FakeTransport().add(json_response([_summary_payload("a")]))
    result = _endpoint(transport).list()

    item = result.items[0]
    assert item.owner_id == "user-1"
    assert item.owner_username == "alice"
    assert item.org_id == "org-1"
    assert item.tags == ["sample"]
    # Trailing-Z timestamps parse to aware datetimes on 3.10.
    assert item.created_at.tzinfo is not None
    assert item.created_at.year == 2026


def test_get_returns_full_item_envelope() -> None:
    transport = FakeTransport().add(json_response(_item_payload("abc")))
    item = _endpoint(transport).get("abc")

    assert path_of(transport.requests[0]) == "/api/items/abc"
    assert item.id == "abc"
    assert item.data["layers"][0]["id"] == "l1"
    assert item.license == "CC-BY-4.0"


def test_create_posts_full_envelope() -> None:
    transport = FakeTransport().add(json_response(_item_payload("new")))
    result = _endpoint(transport).create(
        type="data_layer",
        title="New layer",
        data={"layers": []},
        description="testing",
        tags=["t1"],
        access="org",
    )

    assert result.id == "new"
    sent = body_json(transport.requests[0])
    assert isinstance(sent, dict)
    assert sent["type"] == "data_layer"
    assert sent["title"] == "New layer"
    assert sent["access"] == "org"
    assert sent["description"] == "testing"
    assert sent["tags"] == ["t1"]


def test_update_sends_only_provided_fields() -> None:
    transport = FakeTransport().add(json_response(_item_payload("abc")))
    _endpoint(transport).update("abc", title="Renamed")

    assert transport.requests[0].method == "PATCH"
    assert body_json(transport.requests[0]) == {"title": "Renamed"}


def test_delete_hits_expected_route() -> None:
    transport = FakeTransport().add(json_response(None, status=204))
    _endpoint(transport).delete("abc")

    sent = transport.requests[0]
    assert sent.method == "DELETE"
    assert path_of(sent) == "/api/items/abc"


class TestBboxParsing:
    """The portal's extent, which the QGIS side uses to zoom to a layer.

    The portal sends `[]` (not null) for an item it has no extent for,
    which today is every tile layer, so "no extent" has to be a normal
    outcome rather than a parse failure.
    """

    def _summary(self, bbox: object) -> object:
        from gratisgis_client.models.item import ItemSummary

        payload = _summary_payload()
        payload["bbox"] = bbox
        return ItemSummary.from_api(payload).bbox

    def test_reads_a_real_extent(self) -> None:
        assert self._summary([-79.88, 38.8, -79.72, 38.91]) == (
            -79.88,
            38.8,
            -79.72,
            38.91,
        )

    def test_integers_are_accepted_as_coordinates(self) -> None:
        assert self._summary([-80, 38, -79, 39]) == (-80.0, 38.0, -79.0, 39.0)

    def test_a_point_extent_is_kept_as_given(self) -> None:
        # A single-feature layer is legitimately zero-width. Padding it
        # is the QGIS side's job, since only it knows what a usable
        # zoom looks like.
        assert self._summary([-80.0, 38.0, -80.0, 38.0]) == (-80.0, 38.0, -80.0, 38.0)

    @pytest.mark.parametrize(
        "bbox",
        [
            [],  # what the portal sends for a tile layer
            None,
            "not a bbox",
            [1, 2, 3],
            [1, 2, 3, 4, 5],
            ["a", "b", "c", "d"],
            [True, 2, 3, 4],  # bools are ints in Python; not coordinates
            [float("nan"), 2, 3, 4],
            [float("inf"), 2, 3, 4],
            [10, 2, 3, 4],  # min beyond max
            [1, 10, 3, 4],
        ],
    )
    def test_unusable_values_read_as_no_extent(self, bbox: object) -> None:
        assert self._summary(bbox) is None

    def test_a_missing_key_is_not_an_error(self) -> None:
        from gratisgis_client.models.item import ItemSummary

        payload = _summary_payload()
        payload.pop("bbox", None)
        assert ItemSummary.from_api(payload).bbox is None


def test_summary_to_api_dict_round_trips() -> None:
    # search_dock stashes summaries in Qt item roles as plain dicts
    # and re-hydrates them on click; the round trip must be lossless.
    from gratisgis_client.models.item import ItemSummary

    original = ItemSummary.from_api(_summary_payload("rt"))
    revived = ItemSummary.from_api(original.to_api_dict())
    assert revived == original
