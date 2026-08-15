# SPDX-License-Identifier: AGPL-3.0-or-later
"""Putting the changelog into the shipped metadata.txt.

The changelog is prose written for users, so it can contain anything:
Windows paths, regex characters, currency symbols. It reaches
``re.sub`` as the replacement argument, where a backslash is an escape,
and a release note mentioning ``path\\to\\project.qgz`` failed the
build with "bad escape \\p".

That is a build-time crash rather than a shipped bug, but it fires at
the worst moment: after the commit is pushed and while the release is
half done.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "scripts")
)

from make_zip import _write_changelog_into

_METADATA = (
    "[general]\n"
    "name=GratisGIS\n"
    "version=1.2.3\n"
    "changelog=old text\n"
    "hasProcessingProvider=False\n"
)


@pytest.mark.parametrize(
    "entry",
    [
        r"Run python scripts\repair_project.py path\to\project.qgz",
        r"Matches \d+ items",
        "A literal backslash: \\",
        "Group reference lookalike: \\1 and \\g<name>",
        "Ampersand & in the middle",
        "Percent %s and brace {0}",
    ],
    ids=[
        "windows-path", "digit-escape", "trailing-backslash",
        "group-refs", "ampersand", "format-lookalikes",
    ],
)
def test_any_prose_survives_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """The changelog is text, and must never be parsed as a pattern.

    ``&`` and ``\\1`` are the sharp ones: in a replacement string they
    are references, so they would silently rewrite the release note
    rather than crash, and the wrong text would ship.
    """
    import make_zip

    monkeypatch.setattr(make_zip, "_changelog_field", lambda: entry)
    path = tmp_path / "metadata.txt"
    path.write_text(_METADATA, encoding="utf-8")

    _write_changelog_into(path)

    text = path.read_text(encoding="utf-8")
    assert f"changelog={entry}" in text
    assert "old text" not in text
    # The fields around it are untouched.
    assert "name=GratisGIS" in text
    assert "hasProcessingProvider=False" in text


def test_an_absent_changelog_field_is_appended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import make_zip

    monkeypatch.setattr(make_zip, "_changelog_field", lambda: "fresh")
    path = tmp_path / "metadata.txt"
    path.write_text("[general]\nname=GratisGIS\n", encoding="utf-8")

    _write_changelog_into(path)
    assert "changelog=fresh" in path.read_text(encoding="utf-8")


def test_nothing_to_say_leaves_the_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import make_zip

    monkeypatch.setattr(make_zip, "_changelog_field", lambda: "")
    path = tmp_path / "metadata.txt"
    path.write_text(_METADATA, encoding="utf-8")

    _write_changelog_into(path)
    assert path.read_text(encoding="utf-8") == _METADATA
