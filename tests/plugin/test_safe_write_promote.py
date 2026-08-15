# SPDX-License-Identifier: AGPL-3.0-or-later
"""Getting a staged file into place when the target cannot be renamed.

Matt's second clone of a layer failed with WinError 5 and left him with
no layer at all: the overwrite removes the old layer to release the
file, the rename was refused anyway, and nothing put the layer back.

The rename is refused because reading a GeoPackage through QGIS puts
the dataset in GDAL's pool, and removing the layer from the project
does not empty the pool. Measured in real QGIS: a layer that was merely
OPENED releases its file, one whose features have been READ does not,
and every layer drawn on the canvas has read its features.

Waiting does not help, the lock is not transient. Repointing the
provider elsewhere first does not help. Destroying GDAL's driver
manager to flush the pool takes the process down with an access
violation. Writing the bytes into the existing file does work, because
the handle permits writes and it is only the rename that is refused.

That fallback trades away atomicity, so these tests are mostly about
the price: it is opt-in, and it puts the old contents back if it fails
part way.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from gratisgis_qgis.offline.clone import safe_write_path


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


class TestTheOrdinaryPath:
    def test_a_new_file_is_created(self, tmp_path: Path) -> None:
        final = tmp_path / "clone.gpkg"
        with safe_write_path(str(final)) as tmp:
            _write(tmp, b"fresh")
        assert final.read_bytes() == b"fresh"

    def test_an_existing_file_is_replaced(self, tmp_path: Path) -> None:
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"old")
        with safe_write_path(str(final)) as tmp:
            _write(tmp, b"new")
        assert final.read_bytes() == b"new"

    def test_a_failed_write_leaves_the_previous_copy_alone(
        self, tmp_path: Path
    ) -> None:
        """The property the whole helper exists for.

        The previous copy may hold offline edits that have not been
        pushed, so a failed re-clone must not be able to touch it.
        """
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"precious")
        with pytest.raises(RuntimeError), safe_write_path(str(final)) as tmp:
            _write(tmp, b"partial")
            raise RuntimeError("download died")
        assert final.read_bytes() == b"precious"

    def test_no_staging_directory_is_left_behind(
        self, tmp_path: Path
    ) -> None:
        final = tmp_path / "clone.gpkg"
        with safe_write_path(str(final)) as tmp:
            _write(tmp, b"x")
        assert [p.name for p in tmp_path.iterdir()] == ["clone.gpkg"]


class TestTheInPlaceFallbackIsOptIn:
    """It cannot tell a stale pool entry from a live reader.

    A file held open by GDAL's pool after its layer was removed is safe
    to overwrite; one a live layer is reading right now is not, and
    from inside this helper the two look identical. So the caller has
    to vouch, and only the clone overwrite does, after removing every
    project layer using the file.
    """

    def test_by_default_a_refused_rename_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"locked")
        monkeypatch.setattr(
            os, "replace", _refuse, raising=True
        )
        with pytest.raises(PermissionError), safe_write_path(str(final)) as tmp:
            _write(tmp, b"new")
        assert final.read_bytes() == b"locked", "the target must be untouched"

    def test_with_permission_the_contents_are_written_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"locked")
        monkeypatch.setattr(os, "replace", _refuse, raising=True)
        with safe_write_path(str(final), allow_in_place=True) as tmp:
            _write(tmp, b"new contents")
        assert final.read_bytes() == b"new contents"

    def test_a_shorter_replacement_does_not_leave_a_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writing over without truncating leaves the old file's tail.

        For a GeoPackage that is trailing SQLite pages after the new
        end of file, which GDAL may or may not choke on: a corrupt
        clone that sometimes opens is worse than one that never does.
        """
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"a very long previous version of the file")
        monkeypatch.setattr(os, "replace", _refuse, raising=True)
        with safe_write_path(str(final), allow_in_place=True) as tmp:
            _write(tmp, b"short")
        assert final.read_bytes() == b"short"

    def test_a_missing_target_still_reports_the_rename_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing can be holding a file that does not exist.

        So the rename failed for some other reason, and falling back
        would only hide it behind a second, more confusing error.
        """
        final = tmp_path / "clone.gpkg"
        monkeypatch.setattr(os, "replace", _refuse, raising=True)
        with pytest.raises(PermissionError), safe_write_path(
            str(final), allow_in_place=True
        ) as tmp:
            _write(tmp, b"new")

    def test_a_failed_in_place_write_puts_the_old_contents_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback's one real risk, covered.

        Without this the fallback would be strictly worse than the
        failure it replaces: a refused overwrite leaves the user's copy
        intact, a half-written one destroys it.
        """
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"precious offline edits")
        monkeypatch.setattr(os, "replace", _refuse, raising=True)

        import shutil as shutil_mod

        real_copyfileobj = shutil_mod.copyfileobj

        def die_midway(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            dst.write(b"half")
            raise OSError("disk full")

        monkeypatch.setattr(shutil_mod, "copyfileobj", die_midway)

        with pytest.raises(OSError), safe_write_path(
            str(final), allow_in_place=True
        ) as tmp:
            _write(tmp, b"new contents")

        monkeypatch.setattr(shutil_mod, "copyfileobj", real_copyfileobj)
        assert final.read_bytes() == b"precious offline edits"

    def test_the_fallback_leaves_no_staging_directory_either(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backup lives in the staging directory and goes with it."""
        final = tmp_path / "clone.gpkg"
        final.write_bytes(b"locked")
        monkeypatch.setattr(os, "replace", _refuse, raising=True)
        with safe_write_path(str(final), allow_in_place=True) as tmp:
            _write(tmp, b"new")
        assert [p.name for p in tmp_path.iterdir()] == ["clone.gpkg"]


def _refuse(*_args: object, **_kwargs: object) -> None:
    """Stand in for Windows refusing to rename over an open file."""
    raise PermissionError(5, "Access is denied")
