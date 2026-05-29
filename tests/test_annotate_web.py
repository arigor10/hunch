"""Tests for hunch.annotate_web helpers."""

from __future__ import annotations

from pathlib import Path

from hunch.annotate_web import _find_artifact_snapshot


def _touch(arts_dir: Path, name: str) -> Path:
    p = arts_dir / name
    p.write_text(name)
    return p


def test_find_artifact_by_basename(tmp_path):
    """Hunches store basenames; snapshots are named from the full path."""
    arts = tmp_path / "artifacts"
    arts.mkdir()
    _touch(arts, "_docs_experiments_exp_017_results.md__20260528T000237__c5ac4dcd")
    latest = _touch(
        arts, "_docs_experiments_exp_017_results.md__20260528T031822__7b267642"
    )
    assert _find_artifact_snapshot(arts, "exp_017_results.md") == latest


def test_find_artifact_exact_path_match(tmp_path):
    """A full relative path resolves via the exact-prefix branch."""
    arts = tmp_path / "artifacts"
    arts.mkdir()
    match = _touch(arts, "docs_notes.md__20260528T000237__abc")
    assert _find_artifact_snapshot(arts, "docs/notes.md") == match


def test_find_artifact_picks_latest_by_timestamp(tmp_path):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    _touch(arts, "_a_b_notes.md__20260101T000000__h1")
    latest = _touch(arts, "_a_b_notes.md__20260601T000000__h2")
    assert _find_artifact_snapshot(arts, "notes.md") == latest


def test_basename_does_not_overmatch_different_file(tmp_path):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    _touch(arts, "_docs_exp_017_plan.md__20260528T000237__abc")
    results = _touch(arts, "_docs_exp_017_results.md__20260528T000238__def")
    assert _find_artifact_snapshot(arts, "exp_017_results.md") == results


def test_find_artifact_not_found(tmp_path):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    _touch(arts, "_docs_other.md__20260528T000237__abc")
    assert _find_artifact_snapshot(arts, "missing.md") is None


def test_find_artifact_missing_dir(tmp_path):
    assert _find_artifact_snapshot(tmp_path / "nope", "x.md") is None
