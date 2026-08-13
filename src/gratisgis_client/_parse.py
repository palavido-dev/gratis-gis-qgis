# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small parse helpers behind every model's ``from_api``.

The portal wire shapes need only a handful of checks, and this is
that layer; no validation framework required. Each helper raises
``ValueError`` with the offending key in the message, so a parse
failure names the field (callers such as ``discovery`` already
catch ``ValueError`` and wrap it with request context).

Unknown keys are ignored by construction: ``from_api`` methods read
only the keys they model, which preserves the old ``extra="ignore"``
forward-compatibility contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast


def require_dict(value: Any, context: str) -> dict[str, Any]:
    """Assert the payload is a JSON object before field access.

    Keeps a proxy-mangled response (string body, bare array) failing
    as a clean ``ValueError`` rather than an ``AttributeError`` deep
    inside a ``from_api``.
    """
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected a JSON object, got {type(value).__name__}")
    return cast("dict[str, Any]", value)


def req_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"field {key!r}: expected a string, got {type(value).__name__}")
    return value


def opt_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field {key!r}: expected a string or null, got {type(value).__name__}")
    return value


def _as_int(value: Any, key: str) -> int:
    # bool is an int subclass; a True sneaking in as 1 would mask a
    # portal-side type change, so reject it explicitly.
    if isinstance(value, bool):
        raise ValueError(f"field {key!r}: expected an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        # JSON has one number type; some proxies re-serialize integral
        # values as floats. Accept those, refuse real fractions.
        return int(value)
    raise ValueError(f"field {key!r}: expected an integer, got {type(value).__name__}")


def int_or(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key)
    if value is None:
        return default
    return _as_int(value, key)


def opt_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    return _as_int(value, key)


def req_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"field {key!r}: expected a boolean, got {type(value).__name__}")
    return value


def str_list(data: dict[str, Any], key: str) -> list[str]:
    """A list of strings; missing or null collapses to empty."""
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"field {key!r}: expected a list of strings")
    return cast("list[str]", value)


def dict_or(data: dict[str, Any], key: str) -> dict[str, Any]:
    """A JSON object field; missing or null collapses to empty."""
    value = data.get(key)
    if value is None:
        return {}
    return require_dict(value, f"field {key!r}")


def opt_dict(data: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = data.get(key)
    if value is None:
        return None
    return require_dict(value, f"field {key!r}")


def iso_datetime(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp string into a ``datetime``.

    The portal emits UTC instants with a trailing ``Z``, which
    ``datetime.fromisoformat`` rejects on Python 3.10 (support arrived
    in 3.11). Rewriting it to ``+00:00`` keeps 3.10 compatibility with
    identical semantics.
    """
    if not isinstance(value, str):
        raise ValueError(f"expected an ISO-8601 string, got {type(value).__name__}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp {value!r}: {exc}") from exc


def req_datetime(data: dict[str, Any], key: str) -> datetime:
    value = data.get(key)
    if value is None:
        raise ValueError(f"field {key!r}: required timestamp is missing")
    try:
        return iso_datetime(value)
    except ValueError as exc:
        raise ValueError(f"field {key!r}: {exc}") from exc


def opt_datetime(data: dict[str, Any], key: str) -> datetime | None:
    """A nullable timestamp; missing or null collapses to ``None``."""
    value = data.get(key)
    if value is None:
        return None
    try:
        return iso_datetime(value)
    except ValueError as exc:
        raise ValueError(f"field {key!r}: {exc}") from exc
