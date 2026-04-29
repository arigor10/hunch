"""Tests for hunch bank sync: discovery, ingestion, dedup, conflict
detection, resumability, and legacy labels migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunch.bank.reader import read_bank
from hunch.bank.resolver import resolve_label
from hunch.bank.schema import LIVE_RUN_NAME
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


def _write_feedback(replay_dir: Path, entries: list[dict]) -> None:
    """Write a feedback.jsonl file with explicit channel entries."""
    replay_dir.mkdir(parents=True, exist_ok=True)
    path = replay_dir / "feedback.jsonl"
    with open(path, "w") as f:
        for e in entries:
            event = {
                "ts": e.get("ts", "2026-04-28T00:00:00Z"),
                "hunch_id": e["hunch_id"],
                "channel": e.get("channel", "explicit"),
                "label": e["label"],
                "scientist_reply": e.get("scientist_reply"),
            }
            f.write(json.dumps(event) + "\n")


def _write_replay_hunches(replay_dir: Path, hunches: list[dict]) -> None:
    """Write hunches to .hunch/replay/hunches.jsonl."""
    replay_dir.mkdir(parents=True, exist_ok=True)
    path = replay_dir / "hunches.jsonl"
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

    def test_longest_run_ingested_first(self, tmp_path):
        """Runs are ingested in descending order of hunch count so the
        longest run seeds the bank with the richest canonicals."""
        bank_dir, eval_dir = _setup_project(tmp_path)

        # aaa_short has 1 hunch (alphabetically first)
        _write_hunches(eval_dir / "aaa_short", SAMPLE_HUNCHES[:1])
        # zzz_long has 3 hunches (alphabetically last)
        _write_hunches(eval_dir / "zzz_long", SAMPLE_HUNCHES)

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        # zzz_long should be first despite being alphabetically last
        assert result.runs[0].run_name == "zzz_long"
        assert result.runs[1].run_name == "aaa_short"


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

    def test_duplicate_of_creates_manual_link(self, tmp_path):
        """Labels with duplicate_of should create a manual link event
        mapping the hunch to its duplicate's bank entry."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "original", "bookmark_now": 10},
            {"hunch_id": "h-0002", "smell": "concern A variant",
             "description": "duplicate", "bookmark_now": 15},
            {"hunch_id": "h-0003", "smell": "concern B",
             "description": "different", "bookmark_now": 30},
        ])
        _write_labels(run_dir, [
            {"hunch_id": "h-0001", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z"},
            {"hunch_id": "h-0002", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:01:00Z", "duplicate_of": "h-0001"},
            {"hunch_id": "h-0003", "label": "fp", "source": "evaluator",
             "ts": "2026-04-28T00:02:00Z"},
        ])

        sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)

        state = read_bank(bank_dir / "hunch_bank.jsonl")

        # h-0002 should now be linked to h-0001's bank entry
        bank_id_h1 = state.hunch_to_bank[("run01", "h-0001")]
        bank_id_h2 = state.hunch_to_bank[("run01", "h-0002")]
        assert bank_id_h1 == bank_id_h2

        # h-0002's label should resolve under h-0001's bank entry
        r2 = resolve_label(state, "run01", "h-0002")
        assert r2.label == "tp"
        assert r2.source == "human"

        # h-0003 stays in its own bank entry
        bank_id_h3 = state.hunch_to_bank[("run01", "h-0003")]
        assert bank_id_h3 != bank_id_h1

        # The link event should have source="manual" and replaces_bank_id
        entry = state.entries[bank_id_h1]
        manual_links = [l for l in entry.links if l.source == "manual"]
        assert len(manual_links) == 1
        assert manual_links[0].hunch_id == "h-0002"
        assert manual_links[0].replaces_bank_id is not None

    def test_note_and_tags_preserved(self, tmp_path):
        """Note and tags from labels.jsonl should be preserved in the bank."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        run_dir = eval_dir / "run01"
        _write_hunches(run_dir, SAMPLE_HUNCHES[:1])
        _write_labels(run_dir, [
            {"hunch_id": "h-0001", "label": "tp", "source": "evaluator",
             "ts": "2026-04-28T00:00:00Z",
             "note": "borderline but valuable",
             "tags": ["borderline", "not_novel"]},
        ])

        sync(bank_dir, eval_dir, _never_dup_judge, migrate_labels=True)

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        entry = list(state.entries.values())[0]
        assert len(entry.labels) == 1
        label = entry.labels[0]
        assert label.note == "borderline but valuable"
        assert label.tags == ["borderline", "not_novel"]


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

    def test_two_fresh_runs_single_sync_dedup(self, tmp_path):
        """When two fresh runs are synced in one call against an empty
        bank, the second run should still dedup against the first."""
        bank_dir, eval_dir = _setup_project(tmp_path)

        # Both runs present before sync — bank is empty
        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "first run", "bookmark_now": 10},
            {"hunch_id": "h-0002", "smell": "concern B",
             "description": "first run", "bookmark_now": 20},
        ])
        _write_hunches(eval_dir / "run02", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "second run", "bookmark_now": 12},
            {"hunch_id": "h-0003", "smell": "concern C",
             "description": "second run", "bookmark_now": 40},
        ])

        # Single sync call, empty bank
        result = sync(bank_dir, eval_dir, _smell_match_judge)

        # run01 ingested first (alphabetical), all new entries
        r1 = [r for r in result.runs if r.run_name == "run01"][0]
        assert r1.new_entries == 2
        assert r1.new_links == 0

        # run02: "concern A" should link to run01's entry
        r2 = [r for r in result.runs if r.run_name == "run02"][0]
        assert r2.new_links == 1  # concern A matched
        assert r2.new_entries == 1  # concern C is new

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        assert state.hunch_to_bank[("run02", "h-0001")] == "hb-0001"
        assert len(state.entries) == 3  # A, B, C — not 4


# ---------------------------------------------------------------------------
# Live hunches sync tests
# ---------------------------------------------------------------------------

class TestLiveHunchDiscovery:
    def test_discovers_live_hunches(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:2])

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        live_runs = [r for r in result.runs if r.run_name == LIVE_RUN_NAME]
        assert len(live_runs) == 1
        assert live_runs[0].new_entries == 2
        assert live_runs[0].status == "ingested"

    def test_no_replay_dir_no_live_run(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        result = sync(bank_dir, eval_dir, _never_dup_judge)
        live_runs = [r for r in result.runs if r.run_name == LIVE_RUN_NAME]
        assert len(live_runs) == 0

    def test_live_uses_colon_run_name(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])

        sync(bank_dir, eval_dir, _never_dup_judge)
        state = read_bank(bank_dir / "hunch_bank.jsonl")
        assert (LIVE_RUN_NAME, "h-0001") in state.hunch_to_bank


class TestLiveHunchIncrementalIngest:
    def test_incremental_growth(self, tmp_path):
        """Live hunches grow over time; sync picks up only the new ones."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"

        # First sync: 2 hunches
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:2])
        result1 = sync(bank_dir, eval_dir, _never_dup_judge)
        live1 = [r for r in result1.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live1.new_entries == 2

        # Grow the file: add a third hunch
        with open(replay_dir / "hunches.jsonl", "a") as f:
            event = {
                "type": "emit", "hunch_id": "h-0003",
                "smell": "concern C", "description": "desc C",
                "bookmark_prev": 0, "bookmark_now": 30,
                "emitted_by_tick": 1, "ts": "2026-04-28T00:00:00Z",
                "triggering_refs": {"chunks": [], "artifacts": []},
            }
            f.write(json.dumps(event) + "\n")

        result2 = sync(bank_dir, eval_dir, _never_dup_judge)
        live2 = [r for r in result2.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live2.new_entries == 1
        assert live2.status == "resumed"

    def test_no_growth_is_noop(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:2])

        sync(bank_dir, eval_dir, _never_dup_judge)
        result2 = sync(bank_dir, eval_dir, _never_dup_judge)
        live2 = [r for r in result2.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live2.status == "skipped_up_to_date"

    def test_no_bank_copy_for_live(self, tmp_path):
        """Live hunches are NOT copied to bank/runs/."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])

        sync(bank_dir, eval_dir, _never_dup_judge)
        assert not (bank_dir / "runs" / LIVE_RUN_NAME).exists()


class TestLiveHunchDedup:
    def test_live_dedupes_against_eval_run(self, tmp_path):
        """A live hunch that matches an eval-run bank entry gets linked."""
        bank_dir, eval_dir = _setup_project(tmp_path)

        # Eval run first
        _write_hunches(eval_dir / "run01", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "d", "bookmark_now": 10},
        ])
        sync(bank_dir, eval_dir, _never_dup_judge)

        # Live hunch with same smell
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "live version", "bookmark_now": 12},
        ])
        result = sync(bank_dir, eval_dir, _smell_match_judge)
        live = [r for r in result.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live.new_links == 1
        assert live.new_entries == 0

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        assert state.hunch_to_bank[(LIVE_RUN_NAME, "h-0001")] == "hb-0001"


# ---------------------------------------------------------------------------
# Feedback label import tests
# ---------------------------------------------------------------------------

class TestFeedbackLabelImport:
    def test_imports_good_as_tp(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "good"},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        live = [r for r in result.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live.labels_migrated == 1

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        r = resolve_label(state, LIVE_RUN_NAME, "h-0001")
        assert r.label == "tp"
        assert r.source == "human"

    def test_imports_bad_as_fp(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "bad"},
        ])

        sync(bank_dir, eval_dir, _never_dup_judge)
        state = read_bank(bank_dir / "hunch_bank.jsonl")
        r = resolve_label(state, LIVE_RUN_NAME, "h-0001")
        assert r.label == "fp"

    def test_skips_skip_labels(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "skip"},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        live = [r for r in result.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live.labels_migrated == 0

    def test_skips_implicit_channel(self, tmp_path):
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "implicit",
             "channel": "implicit", "scientist_reply": "interesting"},
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        live = [r for r in result.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live.labels_migrated == 0

    def test_idempotent_feedback_sync(self, tmp_path):
        """Re-syncing with same feedback writes no new labels."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "good"},
        ])

        sync(bank_dir, eval_dir, _never_dup_judge)
        result2 = sync(bank_dir, eval_dir, _never_dup_judge)
        live2 = [r for r in result2.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live2.labels_migrated == 0

    def test_feedback_label_changed(self, tmp_path):
        """If feedback changes, sync writes a new label event."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "good"},
        ])

        sync(bank_dir, eval_dir, _never_dup_judge)

        # Scientist changes mind: good → bad
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "good", "ts": "2026-04-28T00:00:00Z"},
            {"hunch_id": "h-0001", "label": "bad", "ts": "2026-04-28T01:00:00Z"},
        ])
        result2 = sync(bank_dir, eval_dir, _never_dup_judge)
        live2 = [r for r in result2.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live2.labels_migrated == 1

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        r = resolve_label(state, LIVE_RUN_NAME, "h-0001")
        assert r.label == "fp"

    def test_feedback_skipped_for_unknown_hunches(self, tmp_path):
        """Feedback for a hunch not yet in the bank is skipped."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "good"},
            {"hunch_id": "h-9999", "label": "good"},  # not in replay
        ])

        result = sync(bank_dir, eval_dir, _never_dup_judge)
        live = [r for r in result.runs if r.run_name == LIVE_RUN_NAME][0]
        assert live.labels_migrated == 1

    def test_feedback_does_not_delete_file(self, tmp_path):
        """feedback.jsonl is never deleted or renamed."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"
        _write_replay_hunches(replay_dir, SAMPLE_HUNCHES[:1])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "good"},
        ])

        sync(bank_dir, eval_dir, _never_dup_judge)
        assert (replay_dir / "feedback.jsonl").exists()
        assert not (replay_dir / "feedback.jsonl.bak").exists()


class TestFeedbackLabelTierRanking:
    def test_evaluator_label_outranks_feedback_in_inheritance(self, tmp_path):
        """When both live feedback and evaluator labels exist,
        inheritance should prefer the evaluator label."""
        bank_dir, eval_dir = _setup_project(tmp_path)
        replay_dir = tmp_path / ".hunch" / "replay"

        # Live hunch with "good" feedback
        _write_replay_hunches(replay_dir, [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "d", "bookmark_now": 10},
        ])
        _write_feedback(replay_dir, [
            {"hunch_id": "h-0001", "label": "good"},
        ])
        sync(bank_dir, eval_dir, _never_dup_judge)

        # Eval run with same concern (linked), evaluator labels fp
        _write_hunches(eval_dir / "run02", [
            {"hunch_id": "h-0001", "smell": "concern A",
             "description": "eval version", "bookmark_now": 12},
        ])
        _write_labels(eval_dir / "run02", [
            {"hunch_id": "h-0001", "label": "fp", "source": "scientist_retro",
             "ts": "2026-04-28T12:00:00Z"},
        ])
        sync(bank_dir, eval_dir, _smell_match_judge, migrate_labels=True)

        state = read_bank(bank_dir / "hunch_bank.jsonl")

        # A third linked hunch should inherit the evaluator's fp, not the
        # live feedback's tp
        w = BankWriter(bank_dir / "hunch_bank.jsonl")
        from hunch.bank.sync import _now_ts
        w.write_link("hb-0001", "run03", "h-0009", _now_ts(),
                      source="ingest", bookmark_now=11)

        state = read_bank(bank_dir / "hunch_bank.jsonl")
        r = resolve_label(state, "run03", "h-0009")
        assert r.label == "fp"
        assert r.source == "inherited"
        assert r.inherited_from_run == "run02"
