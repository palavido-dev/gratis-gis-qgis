# SPDX-License-Identifier: AGPL-3.0-or-later
"""Endpoint groupings.

Each module here corresponds to one logical area of the portal-api
surface. The top-level ``GratisGISClient`` exposes them as
attributes (``client.items``, ``client.features``, ...).
"""

from gratisgis_client.endpoints.items import ItemsEndpoint

__all__ = ["ItemsEndpoint"]
