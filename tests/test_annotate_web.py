"""Tests for hunch.annotate_web helpers."""

from __future__ import annotations

import json
from pathlib import Path

from hunch.annotate_web import (
    _discover_runs,
    _find_artifact_snapshot,
    _load_bank_items,
    _resolve_run_hunches_path,
)


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


# --- run resolution: bank/runs/ is durable, eval/ is a fallback ---


def _write_run(root: Path, run: str, hunches: list[dict]) -> Path:
    d = root / run
    d.mkdir(parents=True)
    path = d / "hunches.jsonl"
    with open(path, "w") as f:
        for h in hunches:
            f.write(json.dumps(h) + "\n")
    return path


def _emit(hid: str, *, filter_applied: bool = True) -> dict:
    rec = {
        "type": "emit",
        "hunch_id": hid,
        "smell": f"smell {hid}",
        "description": f"desc {hid}",
        "bookmark_prev": 1,
        "bookmark_now": 2,
        "emitted_by_tick": 1,
        "triggering_refs": {},
    }
    if filter_applied:
        rec["filter_applied"] = True
    return rec


def test_resolve_prefers_bank_copy(tmp_path):
    bank_dir = tmp_path / "bank"
    eval_dir = tmp_path / "eval"
    bank_path = _write_run(bank_dir / "runs", "r1", [_emit("h-1")])
    _write_run(eval_dir, "r1", [_emit("h-1")])
    assert _resolve_run_hunches_path("r1", bank_dir, eval_dir) == bank_path


def test_resolve_falls_back_to_eval_for_unsynced(tmp_path):
    bank_dir = tmp_path / "bank"
    eval_dir = tmp_path / "eval"
    (bank_dir / "runs").mkdir(parents=True)
    eval_path = _write_run(eval_dir, "r1", [_emit("h-1")])
    assert _resolve_run_hunches_path("r1", bank_dir, eval_dir) == eval_path


def test_resolve_returns_none_when_absent(tmp_path):
    assert _resolve_run_hunches_path("r1", tmp_path / "bank", tmp_path / "eval") is None


def test_synced_run_survives_eval_deletion(tmp_path):
    """A run synced to the bank still appears after its eval dir is deleted."""
    bank_dir = tmp_path / "bank"
    eval_dir = tmp_path / "eval"
    _write_run(bank_dir / "runs", "synced", [_emit("h-1"), _emit("h-2")])
    eval_dir.mkdir()  # eval exists but the run dir was deleted

    runs = _discover_runs(eval_dir, bank_dir)
    names = [r["name"] for r in runs]
    assert names == ["synced"]
    assert runs[0]["hunch_count"] == 2

    # Content loads from the bank copy even with no eval copy present.
    items = _load_bank_items(None, eval_dir, ["synced"], bank_dir)
    assert {it["hunch_id"] for it in items} == {"h-1", "h-2"}
    assert all(it["unsynced"] for it in items)  # state=None -> no bank mapping


def test_discover_unions_bank_and_eval(tmp_path):
    bank_dir = tmp_path / "bank"
    eval_dir = tmp_path / "eval"
    _write_run(bank_dir / "runs", "synced", [_emit("h-1")])
    _write_run(eval_dir, "unsynced", [_emit("h-9", filter_applied=False)])

    runs = {r["name"]: r for r in _discover_runs(eval_dir, bank_dir)}
    assert set(runs) == {"synced", "unsynced"}
    assert runs["unsynced"]["unfiltered"] == 1
    assert runs["synced"]["unfiltered"] == 0


def test_discover_excludes_synthetic_runs(tmp_path):
    """:live and :mined:* bank runs are not part of the eval surface."""
    bank_dir = tmp_path / "bank"
    eval_dir = tmp_path / "eval"
    _write_run(bank_dir / "runs", "real", [_emit("h-1")])
    _write_run(bank_dir / "runs", ":mined:nose_v2", [_emit("m-1")])
    _write_run(bank_dir / "runs", ":live", [_emit("l-1")])
    eval_dir.mkdir()

    names = [r["name"] for r in _discover_runs(eval_dir, bank_dir)]
    assert names == ["real"]


class _FakeState:
    """Minimal stand-in for BankState: only the fields these helpers touch."""

    def __init__(self, tombstoned: set[str]):
        self.tombstoned_runs = tombstoned
        self.hunch_to_bank: dict[tuple[str, str], str] = {}


def test_discover_excludes_tombstoned_runs(tmp_path):
    bank_dir = tmp_path / "bank"
    eval_dir = tmp_path / "eval"
    _write_run(bank_dir / "runs", "good", [_emit("h-1")])
    _write_run(bank_dir / "runs", "dead", [_emit("h-2")])
    _write_run(eval_dir, "dead", [_emit("h-2")])

    names = [r["name"] for r in _discover_runs(eval_dir, bank_dir, {"dead"})]
    assert names == ["good"]


def test_load_bank_items_skips_tombstoned_runs(tmp_path):
    bank_dir = tmp_path / "bank"
    eval_dir = tmp_path / "eval"
    _write_run(bank_dir / "runs", "dead", [_emit("h-1"), _emit("h-2")])
    eval_dir.mkdir()

    state = _FakeState({"dead"})
    items = _load_bank_items(state, eval_dir, ["dead"], bank_dir)
    assert items == []
