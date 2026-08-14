# SPDX-License-Identifier: AGPL-3.0-or-later
"""Install the built zip into the local QGIS profile.

Saves the build-download-unzip dance during a fix-and-retest loop.
Removes the existing plugin directory first, because leaving stale
files behind is how a deleted module keeps being imported.
"""
from __future__ import annotations

import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from gratisgis_client._version import __version__  # noqa: E402

PLUGIN_NAME = "gratisgis_qgis"


def profile_plugins_dir() -> str:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA is not set; cannot locate the QGIS profile.")
    for qgis_dir in ("QGIS4", "QGIS3"):
        candidate = os.path.join(
            appdata, "QGIS", qgis_dir, "profiles", "default", "python", "plugins"
        )
        if os.path.isdir(os.path.dirname(candidate)) or os.path.isdir(candidate):
            return candidate
    raise SystemExit("No QGIS profile found under %APPDATA%\\QGIS.")


def main() -> int:
    zip_path = os.path.join(ROOT, "dist", f"{PLUGIN_NAME}-{__version__}.zip")
    if not os.path.isfile(zip_path):
        raise SystemExit(f"Build it first: {zip_path} does not exist.")

    plugins_dir = profile_plugins_dir()
    os.makedirs(plugins_dir, exist_ok=True)
    target = os.path.join(plugins_dir, PLUGIN_NAME)

    if os.path.isdir(target):
        shutil.rmtree(target)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(plugins_dir)

    metadata = os.path.join(target, "metadata.txt")
    version_line = ""
    if os.path.isfile(metadata):
        with open(metadata, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("version="):
                    version_line = line.strip()
                    break
    print(f"Installed {PLUGIN_NAME} to {target}")
    print(f"  {version_line or 'version= not found in metadata.txt'}")
    print("Restart QGIS (or use Plugin Reloader) to pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
