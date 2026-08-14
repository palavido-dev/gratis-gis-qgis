#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build a QGIS-installable zip of the gratisgis_qgis plugin.

The QGIS Plugin Repository expects a flat zip where the top-level
directory is the plugin folder and that folder contains
metadata.txt + __init__.py at the root. Our repo layout is
``src/gratisgis_qgis/...`` for clean Python packaging, plus the
``gratisgis_client`` library it depends on. This script:

  1. Stages a fresh build/ directory.
  2. Copies ``src/gratisgis_qgis`` to ``build/gratisgis_qgis``.
  3. Vendors the ``gratisgis_client`` library into
     ``build/gratisgis_qgis/_vendor/gratisgis_client`` so the
     plugin works on a stock QGIS install with no extra pip steps.
     No import rewriting: the plugin's ``__init__`` registers the
     vendored package in ``sys.modules`` under its canonical name
     (see ``_install_vendored_client`` there), so plain
     ``gratisgis_client`` imports resolve to the vendored copy in
     both the plugin's files and the vendored library's own.
  4. Copies the repo LICENSE into the staged plugin directory so
     the zip carries the license text.
  5. Drops test code, ``__pycache__``, and any dev-only artifacts.
  6. Zips the result as ``dist/gratisgis_qgis-<version>.zip``.

Run from the repo root:

    python scripts/make_zip.py

The resulting zip is what you upload to plugins.qgis.org or
hand to a user for manual install via Plugins -> Install from ZIP.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
BUILD_DIR = REPO_ROOT / "build"
DIST_DIR = REPO_ROOT / "dist"

# Directory names we never want inside the plugin zip.
_PRUNE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "tests",
    ".venv",
}

# File patterns to drop from the staged plugin directory.
_PRUNE_FILE_SUFFIXES = (".pyc", ".pyo", ".bak", ".swp", ".swo")


def main() -> int:
    # Sanity: the script assumes a particular layout. Spell it out
    # so a future repo reorg gets a loud failure here, not a
    # silently-broken zip.
    if not (SRC / "gratisgis_qgis" / "metadata.txt").is_file():
        print(
            "ERROR: expected src/gratisgis_qgis/metadata.txt; run from "
            "the repo root.",
            file=sys.stderr,
        )
        return 2

    version = _read_plugin_version()
    print(f"Building gratisgis_qgis plugin v{version}")

    # 1) Fresh build directory.
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    staged_plugin = BUILD_DIR / "gratisgis_qgis"
    shutil.copytree(SRC / "gratisgis_qgis", staged_plugin)

    # 2) Vendor the client library.
    vendor_root = staged_plugin / "_vendor"
    vendor_root.mkdir(exist_ok=True)
    (vendor_root / "__init__.py").write_text(
        "# SPDX-License-Identifier: AGPL-3.0-or-later\n"
        "# Vendored sibling packages for the plugin. Do not import\n"
        "# through this path: the plugin's __init__ registers the\n"
        "# vendored gratisgis_client in sys.modules under its\n"
        "# canonical name, so import it as plain gratisgis_client.\n"
    )
    shutil.copytree(
        SRC / "gratisgis_client",
        vendor_root / "gratisgis_client",
    )
    # No import rewriting here. The plugin's __init__ aliases
    # sys.modules["gratisgis_client"] to the vendored package when
    # the _vendor tree is present, so plain ``gratisgis_client``
    # imports work in the plugin's files and inside the vendored
    # library alike, regardless of the installed plugin folder name.

    # 3) Ship the license text alongside the code, as the AGPL asks.
    shutil.copy2(REPO_ROOT / "LICENSE", staged_plugin / "LICENSE")

    # 4) Prune caches, tests, dev-only files.
    _prune(staged_plugin)
    _prune(vendor_root)

    # 5) Zip.
    zip_path = DIST_DIR / f"gratisgis_qgis-{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(staged_plugin):
            for name in files:
                full = Path(root) / name
                arcname = full.relative_to(BUILD_DIR)
                zf.write(full, arcname.as_posix())

    print(f"Built: {zip_path}")
    print(f"Size:  {zip_path.stat().st_size / 1024:.1f} KB")
    return 0


# Helpers


_VERSION_RE = re.compile(r"^version\s*=\s*([^\s#]+)", re.MULTILINE)


def _read_plugin_version() -> str:
    text = (SRC / "gratisgis_qgis" / "metadata.txt").read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if not match:
        raise RuntimeError("metadata.txt is missing a `version=` line")
    return match.group(1).strip()


def _prune(root: Path) -> None:
    """Walk the staged tree and remove caches + dev junk."""
    # Walk bottom-up so we can rmtree directories without
    # invalidating the iterator.
    for current_root, dirs, files in os.walk(root, topdown=False):
        for d in list(dirs):
            if d in _PRUNE_DIRS:
                shutil.rmtree(Path(current_root) / d, ignore_errors=True)
        for f in files:
            if f.endswith(_PRUNE_FILE_SUFFIXES):
                # A file we cannot delete is dev junk we failed to
                # strip, not a reason to fail the build.
                with contextlib.suppress(OSError):
                    (Path(current_root) / f).unlink()


if __name__ == "__main__":
    raise SystemExit(main())
