# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anti-DOA regression test for the plugin zip.

The v0.1.0 zip shipped dead on arrival: it depended on packages a
stock QGIS install does not have, the build's import rewriting left
the vendored client unresolvable, and nothing in the suite ever
imported what the zip actually contained. This test closes that
hole. It builds the zip with ``scripts/make_zip.py``, checks the
packaging invariants (metadata, LICENSE, vendored client present,
no caches or test code inside), then extracts the zip and imports
every module of it in a fresh subprocess interpreter that has a
stubbed ``qgis`` package and no access to the repo's ``src/`` tree,
which is the resolution environment a user's QGIS provides.

Everything here is standard library plus pytest, so the test runs
the same locally and in CI. It only skips when the repo checkout
layout is missing (for example when the suite is run from an
installed distribution rather than a checkout).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAKE_ZIP = REPO_ROOT / "scripts" / "make_zip.py"
METADATA = REPO_ROOT / "src" / "gratisgis_qgis" / "metadata.txt"
LICENSE = REPO_ROOT / "LICENSE"

pytestmark = pytest.mark.skipif(
    not (MAKE_ZIP.is_file() and METADATA.is_file() and LICENSE.is_file()),
    reason="repo checkout layout not present; the zip build needs scripts/, src/, and LICENSE",
)


@pytest.fixture(scope="module")
def built_zip() -> Path:
    """Build the zip once for the module and return its path."""
    proc = subprocess.run(
        [sys.executable, str(MAKE_ZIP)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"make_zip.py failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    match = re.search(
        r"^version\s*=\s*([^\s#]+)", METADATA.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match, "metadata.txt lost its version line"
    zip_path = REPO_ROOT / "dist" / f"gratisgis_qgis-{match.group(1).strip()}.zip"
    assert zip_path.is_file(), f"expected the built zip at {zip_path}"
    return zip_path


def test_zip_contains_required_files_and_no_dev_junk(built_zip: Path) -> None:
    with zipfile.ZipFile(built_zip) as zf:
        names = zf.namelist()

    required = (
        "gratisgis_qgis/metadata.txt",
        "gratisgis_qgis/__init__.py",
        "gratisgis_qgis/LICENSE",
        "gratisgis_qgis/_vendor/gratisgis_client/__init__.py",
    )
    for entry in required:
        assert entry in names, f"{entry} missing from the zip"

    # QGIS installs the zip's single top-level folder as the plugin
    # directory, so every entry must live under it.
    assert all(n.startswith("gratisgis_qgis/") for n in names)

    # Dev junk in the zip is dead weight at best and (for tests and
    # caches) a broken-import hazard at worst.
    offenders = [
        n
        for n in names
        if "__pycache__" in n or "/tests/" in n or n.endswith((".pyc", ".pyo"))
    ]
    assert not offenders, f"dev junk shipped in the zip: {offenders}"


def test_extracted_zip_imports_everywhere_without_repo_or_deps(
    built_zip: Path, tmp_path: Path
) -> None:
    """Import every module of the extracted zip in a bare interpreter.

    This is the test that would have caught the v0.1.0 DOA zip: the
    subprocess sees only the extracted plugin and a stub ``qgis``
    package, so any module whose imports resolve against the repo
    ``src/`` tree or a pip-installed dependency fails here the way
    it fails on a user's machine.
    """
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(built_zip) as zf:
        zf.extractall(extract_dir)

    stub_dir = tmp_path / "stubs"
    _write_stub_qgis(stub_dir)
    runner = tmp_path / "import_walker.py"
    runner.write_text(_RUNNER_SOURCE, encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSTARTUP")}
    # The plugin's file logger resolves its directory through
    # QStandardPaths at import time; the stub routes it here so the
    # test never writes outside tmp.
    env["GG_ZIP_SMOKE_APPDATA"] = str(tmp_path / "appdata")

    proc = subprocess.run(
        [sys.executable, str(runner), str(extract_dir), str(stub_dir)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"standalone import walk failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    counted = re.search(r"IMPORTED (\d+) modules", proc.stdout)
    assert counted, f"runner did not report a module count:\n{proc.stdout}"
    # The reload half: QGIS reloads a plugin in-process on install or
    # enable, and the first attempt at vendoring crashed on the second
    # load rather than the first.
    assert "RELOADED OK" in proc.stdout, (
        f"plugin did not survive a QGIS-style reload:\n{proc.stdout}\n{proc.stderr}"
    )
    # Guard against a vacuous pass: the plugin plus the vendored
    # client is dozens of modules, so a tiny count means the walk
    # never descended.
    assert int(counted.group(1)) >= 40, f"suspiciously few modules imported:\n{proc.stdout}"


# The stub qgis package and the subprocess import walker. Both are
# written to the tmp dir at run time so the fresh interpreter finds
# them on its own sys.path, entirely outside the repo tree.


def _write_stub_qgis(stub_dir: Path) -> None:
    """Write a minimal fabricating ``qgis`` stub package.

    Same idea as tests/plugin/conftest.py's install_qgis_stub, but
    file-based (a fresh subprocess cannot see monkeypatch) and
    fabricating instead of enumerated: module level ``__getattr__``
    (PEP 562) plus a fabricating metaclass satisfy any name the
    plugin imports or subclasses, so the stub cannot silently rot
    when the plugin gains a new qgis import.
    """
    files = {
        "_gg_qgis_stub.py": _STUB_BASE_SOURCE,
        "qgis/__init__.py": '"""Stub qgis namespace package for the zip smoke test."""\n',
        "qgis/core.py": _FABRICATING_MODULE_SOURCE,
        "qgis/gui.py": _FABRICATING_MODULE_SOURCE,
        "qgis/PyQt/__init__.py": '"""Stub qgis.PyQt package."""\n',
        "qgis/PyQt/QtCore.py": _QTCORE_SOURCE,
        "qgis/PyQt/QtGui.py": _FABRICATING_MODULE_SOURCE,
        "qgis/PyQt/QtWidgets.py": _FABRICATING_MODULE_SOURCE,
        "qgis/PyQt/QtNetwork.py": _FABRICATING_MODULE_SOURCE,
    }
    for rel, content in files.items():
        target = stub_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


_STUB_BASE_SOURCE = '''\
"""Fabricating stand-ins for the qgis / PyQt namespace.

The zip smoke test only proves imports resolve; it never runs Qt.
So every name a plugin module pulls from the qgis namespace just
needs to exist, be subclassable, and tolerate the small amount of
module-scope usage the plugin performs (attribute chains like
Qt.ConnectionType.QueuedConnection, enum resolution via hasattr,
and the occasional arithmetic or bitwise combination). A metaclass
that fabricates class attributes on demand covers all of that
without hand-listing the qgis API surface.
"""

_CACHE = {}


class _StubMeta(type):
    def __getattr__(cls, name):
        # Dunder lookups must fail normally: fabricating names like
        # __set_name__ or __init_subclass__ would change how Python
        # treats these classes during class creation.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        value = make_class(f"{cls.__name__}.{name}")
        setattr(cls, name, value)
        return value

    # Enum-ish values sometimes get combined or coerced at module
    # scope; keep those expressions alive without modelling Qt.
    def __or__(cls, other):
        return cls

    __ror__ = __or__

    def __and__(cls, other):
        return cls

    __rand__ = __and__

    def __add__(cls, other):
        return cls

    __radd__ = __add__

    def __invert__(cls):
        return cls

    def __int__(cls):
        return 0

    __index__ = __int__


def _permissive_init(self, *args, **kwargs):
    pass


def _permissive_call(self, *args, **kwargs):
    return self


def _instance_getattr(self, name):
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    return make_class(name)


def make_class(name):
    cls = _CACHE.get(name)
    if cls is None:
        cls = _StubMeta(
            name.replace(".", "_"),
            (),
            {
                "__init__": _permissive_init,
                "__call__": _permissive_call,
                "__getattr__": _instance_getattr,
            },
        )
        _CACHE[name] = cls
    return cls


def fabricate(module_globals, name):
    """PEP 562 hook body for the stub qgis submodules."""
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    value = make_class(name)
    module_globals[name] = value
    return value
'''

_FABRICATING_MODULE_SOURCE = '''\
"""Stub module: fabricates any imported name (PEP 562)."""
from _gg_qgis_stub import fabricate


def __getattr__(name):
    return fabricate(globals(), name)
'''

_QTCORE_SOURCE = '''\
"""Stub QtCore: fabricates everything except QStandardPaths.

The plugin's file logger calls QStandardPaths.writableLocation at
import time and hands the result to pathlib and open(), so this one
name needs a real implementation returning a real directory; a
fabricated class object there would crash every module import.
"""
import os
import tempfile

from _gg_qgis_stub import fabricate


class QStandardPaths:
    class StandardLocation:
        AppDataLocation = 0

    @staticmethod
    def writableLocation(_kind):
        return os.environ.get(
            "GG_ZIP_SMOKE_APPDATA",
            os.path.join(tempfile.gettempdir(), "gg-zip-smoke-appdata"),
        )


def __getattr__(name):
    return fabricate(globals(), name)
'''

_RUNNER_SOURCE = '''\
"""Import every module of the extracted plugin zip.

Run as: python import_walker.py <extract_dir> <stub_dir>

Exits non-zero, with a traceback per module, if anything fails to
import. Also asserts that both the plugin and the client resolved
from inside the extracted zip, so a pip-installed or repo copy
sneaking onto sys.path cannot fake a pass.
"""
import importlib
import os
import pkgutil
import sys
import traceback


def _under(path, root):
    real = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    return real == root or real.startswith(root + os.sep)


def main(extract_dir, stub_dir):
    # Stub before extract dir so a real qgis install (possible on a
    # dev box, never in CI) cannot shadow the stub.
    sys.path.insert(0, extract_dir)
    sys.path.insert(0, stub_dir)

    try:
        import gratisgis_qgis
    except BaseException:
        print("FAILED to import gratisgis_qgis itself:", file=sys.stderr)
        traceback.print_exc()
        return 1

    if not _under(gratisgis_qgis.__file__ or "", extract_dir):
        print(
            f"gratisgis_qgis resolved outside the extracted zip: {gratisgis_qgis.__file__}",
            file=sys.stderr,
        )
        return 1

    # The package import must have aliased the vendored client to
    # its canonical name; the DOA zip failed exactly here.
    try:
        import gratisgis_client
    except BaseException:
        print("FAILED to import gratisgis_client through the vendor alias:", file=sys.stderr)
        traceback.print_exc()
        return 1

    if not _under(gratisgis_client.__file__ or "", extract_dir):
        print(
            f"gratisgis_client is not the vendored copy: {gratisgis_client.__file__}",
            file=sys.stderr,
        )
        return 1

    failures = []
    names = []

    def on_walk_error(name):
        # walk_packages swallows package import errors unless told
        # otherwise; a swallowed error here would be a silent pass.
        failures.append((name, traceback.format_exc()))

    for _finder, name, _is_pkg in pkgutil.walk_packages(
        gratisgis_qgis.__path__, prefix="gratisgis_qgis.", onerror=on_walk_error
    ):
        names.append(name)

    for name in names:
        try:
            importlib.import_module(name)
        except BaseException:
            failures.append((name, traceback.format_exc()))

    if failures:
        for name, tb in failures:
            print(f"IMPORT FAILED: {name}\\n{tb}", file=sys.stderr)
        print(f"{len(failures)} of {len(names) + 1} modules failed to import", file=sys.stderr)
        return 1

    print(f"IMPORTED {len(names) + 1} modules")

    # Second load, simulating a QGIS plugin reload. QGIS does not clear
    # sys.modules wholesale: qgis.utils replaces builtins.__import__,
    # records every name a plugin imports, and on unload deletes only
    # that recorded set. Modules the plugin registers directly in
    # sys.modules (our vendored-client alias) were never observed by
    # that hook, so they SURVIVE. Reproducing that asymmetry here is
    # what makes this catch the reload crash without needing QGIS: a
    # surviving gratisgis_qgis._vendor.gratisgis_client used to make
    # find_spec skip importing its parent, and the reload died with
    # KeyError: 'gratisgis_qgis._vendor'.
    # Purge what QGIS's hook would have RECORDED, and nothing else.
    # It records names imported through builtins.__import__ whose root
    # package is the plugin, so gratisgis_qgis and its normally
    # imported submodules go. Two families deliberately stay:
    #   - gratisgis_qgis._vendor.gratisgis_client, injected directly
    #     into sys.modules and therefore never seen by the hook;
    #   - gratisgis_client*, whose root package is not a plugin name.
    # Popping the first of those would defeat the whole test: it is the
    # survivor that triggers the find_spec fast path.
    survives = "gratisgis_qgis._vendor.gratisgis_client"
    for name in list(sys.modules):
        if name != "gratisgis_qgis" and not name.startswith("gratisgis_qgis."):
            continue
        if name == survives or name.startswith(survives + "."):
            continue
        sys.modules.pop(name, None)

    if survives not in sys.modules:
        print(f"test bug: {survives} should have survived the purge", file=sys.stderr)
        return 1

    try:
        import gratisgis_qgis as reloaded
    except BaseException:
        print("FAILED to re-import gratisgis_qgis after a QGIS-style purge:", file=sys.stderr)
        traceback.print_exc()
        return 1

    if not _under(reloaded.__file__ or "", extract_dir):
        print(f"reloaded plugin resolved outside the zip: {reloaded.__file__}", file=sys.stderr)
        return 1

    print("RELOADED OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
'''
