# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single source of the package version.

Lives in its own module (rather than ``__init__.py``) so that
``config.py`` and ``discovery.py`` can build their User-Agent strings
from it without importing the package root, which would be a circular
import. ``pyproject.toml`` reads the version from here at build time
via hatch's version plugin, and ``__init__.py`` re-exports it, so
there is exactly one place to bump.
"""

__version__ = "0.13.0"
