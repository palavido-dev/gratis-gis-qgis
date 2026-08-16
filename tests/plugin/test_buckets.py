# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Browser bucket filter (`browser.buckets`)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gratisgis_client.models.item import ItemSummary
from gratisgis_qgis.browser.buckets import BucketKind, filter_for_bucket


def _item(
    *,
    id: str,
    title: str,
    access: str,
    owner: str,
    type: str = "data_layer",
) -> ItemSummary:
    """Compact factory; defaults to a data_layer item with deterministic
    timestamps so test output stays diffable.
    """
    return ItemSummary(
        id=id,
        type=type,  # type: ignore[arg-type]
        title=title,
        access=access,  # type: ignore[arg-type]
        owner_id=owner,
        org_id="org-1",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# A realistic mixed result: caller "matt" owns one private + one
# public, an org-shared item came from someone else, and a
# leftover public item is owned by yet another user.
@pytest.fixture()
def mixed_roster() -> list[ItemSummary]:
    return [
        _item(id="1", title="my-priv",   access="private", owner="matt"),
        _item(id="2", title="my-pub",    access="public",  owner="matt"),
        _item(id="3", title="org-other", access="org",     owner="alice"),
        _item(id="4", title="org-mine",  access="org",     owner="matt"),
        _item(id="5", title="pub-bob",   access="public",  owner="bob"),
    ]


def test_public_bucket_returns_only_public_items(mixed_roster: list[ItemSummary]) -> None:
    out = filter_for_bucket(mixed_roster, BucketKind.PUBLIC)
    assert {i.title for i in out} == {"my-pub", "pub-bob"}


def test_org_bucket_returns_org_plus_public(mixed_roster: list[ItemSummary]) -> None:
    out = filter_for_bucket(mixed_roster, BucketKind.ORG)
    assert {i.title for i in out} == {"my-pub", "org-other", "org-mine", "pub-bob"}


def test_mine_bucket_returns_caller_owned_items(mixed_roster: list[ItemSummary]) -> None:
    out = filter_for_bucket(mixed_roster, BucketKind.MINE)
    # Inferred caller-id is the most common owner_id among private
    # items -> "matt" -> all matt-owned items, regardless of scope.
    assert {i.title for i in out} == {"my-priv", "my-pub", "org-mine"}


def test_shared_bucket_returns_org_items_not_owned_by_caller(
    mixed_roster: list[ItemSummary],
) -> None:
    out = filter_for_bucket(mixed_roster, BucketKind.SHARED)
    assert {i.title for i in out} == {"org-other"}


def test_mine_returns_empty_when_no_private_items_to_infer_from() -> None:
    # Without any access=private rows the caller-id inference
    # bails out rather than misattributing the roster -- a result
    # of nothing-but-public items can't tell us who's signed in,
    # and labeling random items as "mine" would be worse than
    # showing an empty bucket.
    items = [
        _item(id="1", title="p1", access="public", owner="alice"),
        _item(id="2", title="p2", access="public", owner="bob"),
    ]
    assert filter_for_bucket(items, BucketKind.MINE) == []


def test_shared_returns_empty_when_no_private_items_to_infer_from() -> None:
    items = [
        _item(id="1", title="p1", access="public", owner="alice"),
        _item(id="2", title="o1", access="org",    owner="bob"),
    ]
    assert filter_for_bucket(items, BucketKind.SHARED) == []


def test_unknown_bucket_returns_empty(mixed_roster: list[ItemSummary]) -> None:
    assert filter_for_bucket(mixed_roster, "garbage") == []


def test_empty_roster_returns_empty_for_every_bucket() -> None:
    for kind in (BucketKind.MINE, BucketKind.SHARED, BucketKind.ORG, BucketKind.PUBLIC):
        assert filter_for_bucket([], kind) == []


def test_mine_with_explicit_caller_id_beats_inference(
    mixed_roster: list[ItemSummary],
) -> None:
    out = filter_for_bucket(mixed_roster, BucketKind.MINE, caller_id="alice")
    assert {i.title for i in out} == {"org-other"}


def test_mine_with_caller_id_works_without_private_items() -> None:
    # The regression the caller_id parameter exists for: a user who
    # owns only org / public items has nothing for the inference to
    # latch onto, but the token's sub claim still identifies them.
    items = [
        _item(id="1", title="my-org", access="org", owner="matt"),
        _item(id="2", title="my-pub", access="public", owner="matt"),
        _item(id="3", title="other", access="org", owner="alice"),
    ]
    out = filter_for_bucket(items, BucketKind.MINE, caller_id="matt")
    assert {i.title for i in out} == {"my-org", "my-pub"}


def test_shared_with_caller_id_works_without_private_items() -> None:
    items = [
        _item(id="1", title="my-org", access="org", owner="matt"),
        _item(id="2", title="other", access="org", owner="alice"),
    ]
    out = filter_for_bucket(items, BucketKind.SHARED, caller_id="matt")
    assert {i.title for i in out} == {"other"}


class TestItemTooltip:
    """The hover card: metadata, not mystery (and never a fetch)."""

    def test_it_names_kind_audience_date_and_id(self) -> None:
        from gratisgis_qgis.browser.buckets import item_tooltip

        text = item_tooltip(
            _item(id="i-1", title="Parcels", access="org", owner="u1")
        )
        assert "Data layer" in text
        assert "My organization" in text
        assert "Updated 2026-" in text
        assert "Item id: " in text, (
            "the clone Processing algorithm takes an item id; the "
            "tooltip is where you copy it from"
        )

    def test_a_leafs_own_lead_line_comes_first(self) -> None:
        from gratisgis_qgis.browser.buckets import item_tooltip

        text = item_tooltip(
            _item(id="i-2", title="WV", access="private", owner="u1",
                  type="map"),
            "Double-click to open.",
        )
        assert text.splitlines()[0] == "Double-click to open."
        assert "Map" in text

    def test_an_unknown_type_reads_as_words(self) -> None:
        from gratisgis_qgis.browser.buckets import item_tooltip

        text = item_tooltip(
            _item(id="i-3", title="X", access="private", owner="u1",
                  type="widget_package")
        )
        assert "Widget package" in text

    def test_the_access_labels_agree_with_the_sharing_dialog(self) -> None:
        """Two surfaces name the same audiences; drift here would show
        one wording on hover and another in the dialog."""
        from gratisgis_qgis.browser.buckets import ACCESS_LABELS

        labels = {
            "private": "Only me",
            "org": "My organization",
            "public": "Everyone",
        }
        assert labels == ACCESS_LABELS
