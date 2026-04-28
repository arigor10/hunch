"""Tests for hunch bank sync: discovery, ingestion, dedup, conflict
detection, resumability, and legacy labels migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunch.bank.reader import read_bank
from hunch.bank.resolver import resolve_label
from hunch.bank.sync import sync, migrate_labels
from hunch.bank.writer import BankWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create .hunch/bank/ and .hunch/eval/ dirs, return (bank_dir, eval_dir)."""
    bank_dir = tmp_path / ".hunch" / "bank"
    eval_dir = tmp_path / ".hunch" / "eval"
    bank_dir.mkdir(parents=True)
    eval_dir.mkdir(parents=True)
    return bank_dir, eval_dir


def _write_hunches(run_dir: Path, hunches: list[dict]) -> None:
    """Write a hunches.jsonl file with a meta header + emit events."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "hunches.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"type": "meta", "note": "test"}) + "\n")
        for h in hunches:
            event = {
                "type": "emit",
                "hunch_id": h["hunch_id"],
                "smell": h["smell"],
                "description": h.get("description", ""),
                "bookmark_prev": h.get("bookmark_prev", 0),
                "bookmark_now": h.get("bookmark_now", 0),
                "emitted_by_tick": h.get("emitted_by_tick", 1),
                "ts": h.get("ts", "2026-04-28T00:00:00Z"),
                "triggering_refs": h.get("triggering_refs", {"chunks": [], "artifacts": []}),
            }
            f.write(json.dumps(event) + "\n")


def _write_labels(run_dir: Path, labels: list[dict]) -> None:
    """Write a labels.jsonl file."""
    path = run_dir / "labels.jsonl"
    with open(path, "w") as f:
        for l in labels:
            f.write(json.dumps(l) + "\n")


def _never_dup_judge(smell_a, desc_a, smell_b, desc_b) -> dict:
    """Judge that never finds duplicates."""
    return {"duplicate": False, "reasoning": "different"}


def _always_dup_judge(smell_a, desc_a, smell_b, desc_b) -> dict:
    """Judge that always finds duplicates."""
    return {"duplicate": True, "reasoning": "same", "score": 0.95}


def _smell_match_judge(smell_a, desc_a, smell_b, desc_b) -> dict:
    """Judge that finds duplicates when smells are identical."""
    if smell_a == smell_b:
        return {"duplicate": True, "reasoning": "same smell", "score": 0.99}
    return {"duplicate": False, "reasoning": "different smell"}


SAMPLE_HUNCHES = [
    {"hunch_id": "h-0001", "smell": "concern A", "description": "desc A",
     "bookmark_now": 10},
    {"hunch_id": "h-0002", "smell": "concern B", "description": "desc B",
     "bookmark_now": 20},
    {"hunch_id": "h-0003", "smell": "concern C", "description": "desc C",
     "bookmark_now": 30},
]


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discovers_runs(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES[:1])
        _write_hunches(eval_dir / "run02", SAMPLE_HUNCHES[1:2])

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        assert len(result.runs) == 2
        run_names = {r.run_name for r in result.runs}
        assert run_names == {"run01", "run02"}

    def test_specific_run(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES[:1])
        _write_hunches(eval_dir / "run02", SAMPLE_HUNCHES[1:2])

        result = sync(bank_dir, eval_dir, _never_dup_judge, run_name="run01")
        assert len(result.runs) == 1
        assert result.runs[0].run_name == "run01"

    def test_no_runs(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        result = sync(bank_dir, eval_dir, _never_dup_judge)
        assert len(result.runs) == 0

    def test_ignores_dirs_without_hunches(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        (eval_dir / "empty_run").mkdir()
        result = sync(bank_dir, eval_dir, _never_dup_judge)
        assert len(result.runs) == 0


# ---------------------------------------------------------------------------
# Fresh ingest tests
# ---------------------------------------------------------------------------

class TestFreshIngest:
    def test_all_new_entries(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES)

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        assert result.runs[0].status == "ingested"
        assert result.runs[0].new_entries == 3
        assert result.runs[0].new_links == 0

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        assert len(state.entries) == 3
        assert ("run01", "h-0001") in state.hunch_to_bank

    def test_hunches_copied_to_bank_runs(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES[:1])

        sync(bank_dir, eval_dir, _never_dup_judge)
        assert (bank_dir / "runs" / "run01" / "hunches.jsonl").exists()

    def test_bookmark_now_preserved(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES[:1])

        sync(bank_dir, eval_dir, _never_dup_judge)
        state = read_bank(bank_dir / "hunch_bank.jsonl")
        entry = list(state.entries.values())[0]
        assert entry.bookmark_now == 10


# ---------------------------------------------------------------------------
# Dedup matching tests
# ---------------------------------------------------------------------------

class TestDedupMatching:
    def test_links_to_existing_entry(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)

        # First run
        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0001", "smell": "concern A", "description": "desc A",
             "bookmark_now": 10},
        ])
        sync(bank_dir, eval_dir, _never_dup_judge)

        # Second run with same concern near same bookmark
        _write_hunches(eval_dir / "run02", [
            {"hunch_id": "h-0001", "smell": "concern A", "description": "desc A v2",
             "bookmark_now": 12},
        ])
        result = sync(bank_dir, eval_dir, _smell_match_judge)
        run02_result = [r for r in result.runs if r.run_name == "run02"][0]
        assert run02_result.new_links == 1
        assert run02_result.new_entries == 0

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        assert state.hunch_to_bank[("run02", "h-0001")] == "hb-0001"

    def test_no_match_creates_new_entry(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)

        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES[:1])
        sync(bank_dir, eval_dir, _never_dup_judge)

        _write_hunches(eval_dir / "run02", [
            {"hunch_id": "h-0001", "smell": "totally different",
             "description": "different desc", "bookmark_now": 12},
        ])
        result = sync(bank_dir, eval_dir, _never_dup_judge)
        run02_result = [r for r in result.runs if r.run_name == "run02"][0]
        assert run02_result.new_entries == 1
        assert run02_result.new_links == 0

    def test_best_match_wins(self, tmp_path):
        """When a new hunch matches multiple bank entries, highest score wins."""
        bank_dir, eval_dir = _setup_project(tmp_path)

        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "d", "bookmark_now": 10},
            {"hunch_id": "h-0002", "smell": "concern A variant",
             "description": "d", "bookmark_now": 12},
        ])
        sync(bank_dir, eval_dir, _never_dup_judge)

        call_count = [0]

        def _scoring_judge(sa, da, sb, db):
            call_count[0] += 1
            return {"duplicate": True, "reasoning": "match",
                    "score": 0.9 if "variant" in sa else 0.5}

        _write_hunches(eval_dir / "run02", [
            {"hunch_id": "h-0001", "smell": "concern A again",
             "description": "d", "bookmark_now": 11},
        ])
        sync(bank_dir, eval_dir, _scoring_judge)

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        # Should link to hb-0002 (higher score from "variant" match)
        assert state.hunch_to_bank[("run02", "h-0001")] == "hb-0002"

    def test_windowed_comparison(self, tmp_path):
        """Hunches far from any bank entry's bookmark are not compared.

        The window is ±k items in the sorted array. We need enough
        hunches that the far one falls outside the window.
        """
        bank_dir, eval_dir = _setup_project(tmp_path)

        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "d", "bookmark_now": 10},
        ])
        sync(bank_dir, eval_dir, _never_dup_judge)

        calls = []

        def _tracking_judge(sa, da, sb, db):
            calls.append((sa, sb))
            return {"duplicate": False}

        # Create 20 hunches; bank entry at bookmark 10, window k=2.
        # Only hunches near bookmark 10 (indices near the bisect point)
        # should be compared.
        run02_hunches = [
            {"hunch_id": f"h-{i:04d}", "smell": f"hunch-{i}",
             "description": "d", "bookmark_now": i * 10}
            for i in range(1, 21)
        ]
        _write_hunches(eval_dir / "run02", run02_hunches)
        sync(bank_dir, eval_dir, _tracking_judge, window_k=2)

        # Bank entry at bookmark 10 → bisect lands near the start.
        # With k=2, at most 4 hunches compared. Hunch at bookmark 200
        # should NOT be compared.
        compared_smells = {sb for _, sb in calls}
        assert len(compared_smells) <= 4
        assert "hunch-20" not in compared_smells  # bookmark 200, far away


# ---------------------------------------------------------------------------
# Idempotency and resumability tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_sync_twice_is_noop(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES)

        sync(bank_dir, eval_dir, _never_dup_judge)
        result2 = sync(bank_dir, eval_dir, _never_dup_judge)

        assert result2.runs[0].status == "skipped_up_to_date"
        assert result2.total_entries == 0

    def test_resume_after_partial_ingest(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES)

        # Simulate partial ingest: manually write one entry
        writer = BankWriter(bank_dir / "hunch_bank.jsonl")
        writer.write_entry(
            "hb-0001", "concern A", "desc A", "run01", "h-0001",
            "2026-04-28T00:00:01Z", bookmark_now=10,
        )
        # Copy hunches file
        (bank_dir / "runs" / "run01").mkdir(parents=True)
        import shutil
        shutil.copy2(eval_dir / "run01" / "hunches.jsonl",
                      bank_dir / "runs" / "run01" / "hunches.jsonl")

        # Sync should resume and process h-0002 and h-0003
        result = sync(bank_dir, eval_dir, _never_dup_judge)
        assert result.runs[0].status == "resumed"
        assert result.runs[0].new_entries == 2
        assert result.runs[0].hunches_processed == 2


# ---------------------------------------------------------------------------
# Conflict detection tests
# ---------------------------------------------------------------------------

class TestConflictDetection:
    def test_changed_hunches_detected(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES)

        sync(bank_dir, eval_dir, _never_dup_judge)

        # Overwrite with different hunches
        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0099", "smell": "new concern",
             "description": "d", "bookmark_now": 50},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        assert result.runs[0].status == "skipped_conflict"
        assert "changed since ingestion" in result.runs[0].conflict_detail

    def test_identical_hunches_not_conflict(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        _write_hunches(eval_dir / "run01", SAMPLE_HUNCHES)

        sync(bank_dir, eval_dir, _never_dup_judge)

        # Sync again — same file, no conflict
        result = sync(bank_dir, eval_dir, _never_dup_judge)
        assert result.runs[0].status == "skipped_up_to_date"


# ---------------------------------------------------------------------------
# Legacy labels migration tests
# ---------------------------------------------------------------------------

class TestLabelsMigration:
    def test_migrate_labels_auto(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, SAMPLE_HUNCHES[:2])
        _write_labels(run_dir, [
            {"hunch_id": "h-0001", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
            {"hunch_id": "h-0002", "label": "fp", "source": "evaluator",
             "ts": "2026-04-28T00:01:00Z"},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)
        assert result.runs[0].labels_migrated == 2
        assert (run_dir / "labels.jsonl.bak").exists()
        assert not (run_dir / "labels.jsonl").exists()

        # Verify labels in bank
        state = read_bank(bank_dir / "hunch_bank.jsonl")
        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.label == "tp"
        assert r1.source == "human"
        r2 = resolve_label(state, "run01", "h-0002")
        assert r2.label == "fp"

    def test_labels_pending_when_not_auto(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, SAMPLE_HUNCHES[:1])
        _write_labels(run_dir, [
            {"hunch_id": "h-0001", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=False)
        assert result.runs[0].labels_pending is True
        assert result.runs[0].labels_migrated == 0
        # File should still exist
        assert (run_dir / "labels.jsonl").exists()

    def test_already_migrated_labels_skipped(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, SAMPLE_HUNCHES[:1])
        _write_labels(run_dir, [
            {"hunch_id": "h-0001", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
        ])

        # First sync migrates
        sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)
        # Second sync should not attempt migration again
        result2 = sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)
        assert result2.runs[0].labels_migrated == 0
        assert result2.runs[0].labels_pending is False

    def test_skip_labels_not_in_bank(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, SAMPLE_HUNCHES[:1])
        _write_labels(run_dir, [
            {"hunch_id": "h-9999", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)
        assert result.runs[0].labels_migrated == 0

    def test_skip_non_tp_fp_labels(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, SAMPLE_HUNCHES[:1])
        _write_labels(run_dir, [
            {"hunch_id": "h-0001", "label": "skip", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)
        assert result.runs[0].labels_migrated == 0


# ---------------------------------------------------------------------------
# Dormant entry revival via sync
# ---------------------------------------------------------------------------

class TestDormantEntryRevival:
    def test_sync_links_to_dormant_entry(self, tmp_path):
        """A dormant bank entry (source run tombstoned, no live links)
        should still participate in dedup matching. If a new run
        rediscovers the same concern, it links to the dormant entry,
        reviving it."""
        bank_dir, eval_dir = _setup_project(tmp_path)

        # Run01: creates bank entry
        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "d", "bookmark_now": 10},
        ])
        sync(bank_dir, eval_dir, _never_dup_judge)

        # Tombstone run01 → entry becomes dormant
        from hunch.bank.sync import _now_ts
        writer = BankWriter(bank_dir / "hunch_bank.jsonl")
        writer.write_tombstone("run01", _now_ts())

        # Run02: same concern near same bookmark
        _write_hunches(eval_dir / "run02", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "d v2", "bookmark_now": 12},
        ])
        result = sync(bank_dir, eval_dir, _smell_match_judge)
        run02_result = [r for r in result.runs if r.run_name == "run02"][0]
        assert run02_result.new_links == 1
        assert run02_result.new_entries == 0

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        assert state.hunch_to_bank[("run02", "h-0001")] == "hb-0001"


# ---------------------------------------------------------------------------
# Interrupted label migration
# ---------------------------------------------------------------------------

class TestInterruptedMigration:
    def test_resumes_after_interrupted_migration(self, tmp_path):
        """If migration was interrupted (both .jsonl and .bak exist),
        sync should re-attempt migration rather than skipping."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, SAMPLE_HUNCHES[:1])
        _write_labels(run_dir, [
            {"hunch_id": "h-0001", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
        ])

        # Simulate interrupted migration: both files exist
        import shutil
        shutil.copy2(run_dir / "labels.jsonl", run_dir / "labels.jsonl.bak")

        # Sync should still migrate
        result = sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)
        assert result.runs[0].labels_migrated == 1
        assert not (run_dir / "labels.jsonl").exists()


# ---------------------------------------------------------------------------
# End-to-end: ingest + dedup + labels
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_two_runs_with_dedup_and_label_inheritance(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)

        # Run01: 3 hunches
        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "first run", "bookmark_now": 10},
            {"hunch_id": "h-0002", "smell": "concern B",
             "description": "first run", "bookmark_now": 20},
            {"hunch_id": "h-0003", "smell": "concern C",
             "description": "first run", "bookmark_now": 30},
        ])
        _write_labels(eval_dir / "run01", [
            {"hunch_id": "h-0001", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
            {"hunch_id": "h-0002", "label": "fp", "source": "evaluator",
             "ts": "2026-04-28T00:01:00Z"},
        ])

        sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)

        # Run02: 2 hunches, one duplicates run01's concern A
        _write_hunches(eval_dir / "run02", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "second run", "bookmark_now": 11},
            {"hunch_id": "h-0002", "smell": "concern D",
             "description": "second run", "bookmark_now": 50},
        ])

        result = sync(bank_dir, eval_dir, _smell_match_judge)

        state = read_bank(bank_dir / "hunch_bank.jsonl")

        # run02/h-0001 linked to hb-0001 (same smell)
        assert state.hunch_to_bank[("run02", "h-0001")] == "hb-0001"

        # run02/h-0001 inherits tp from run01
        r = resolve_label(state, "run02", "h-0001")
        assert r.label == "tp"
        assert r.source == "inherited"

        # run02/h-0002 is new (concern D)
        r2 = resolve_label(state, "run02", "h-0002")
        assert r2.source == "unlabeled"

        # Total entries: 3 from run01 + 1 new from run02 = 4
        assert len(state.entries) == 4
