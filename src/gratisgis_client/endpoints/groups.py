# SPDX-License-Identifier: AGPL-3.0-or-later
"""Groups endpoint: the org's groups, as sharing targets.

Read-only on purpose. The plugin's sharing dialog needs the list of
groups a user can share an item with; creating and administering
groups stays a portal-side activity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gratisgis_client.http import PortalHttp


@dataclass(frozen=True)
class GroupSummary:
    id: str
    name: str

    @classmethod
    def from_api(cls, data: Any) -> GroupSummary | None:
        """A tolerant parse: a group row without an id or name is
        useless as a sharing target, so it reads as absent rather
        than raising over a field this client never touches."""
        if not isinstance(data, dict):
            return None
        group_id = str(data.get("id") or "")
        name = str(data.get("name") or data.get("title") or "")
        if not group_id:
            return None
        return cls(id=group_id, name=name or group_id)


class GroupsEndpoint:
    def __init__(self, http: PortalHttp) -> None:
        self._http = http

    def list(self) -> list[GroupSummary]:
        """Groups visible to the signed-in user, portal order."""
        body = self._http.request_json("GET", "/groups")
        rows = body if isinstance(body, list) else []
        out: list[GroupSummary] = []
        for row in rows:
            parsed = GroupSummary.from_api(row)
            if parsed is not None:
                out.append(parsed)
        return out
