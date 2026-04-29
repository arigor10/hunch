"""Tests for the hunch bank: writer, reader, and label resolver.

Test scenarios S1–S13 from docs/hunch_bank_design.md, plus writer
guard tests for ID allocation and timestamp monotonicity.
"""

from __future__ import annotations

import pytest

from hunch.bank.reader import read_bank
from hunch.bank.resolver import resolve_label
from hunch.bank.writer import BankWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bank_path(tmp_path):
    return tmp_path / "bank" / "hunch_bank.jsonl"


def _writer(tmp_path) -> BankWriter:
    return BankWriter(_bank_path(tmp_path))


def _ts(n: int) -> str:
    """Generate monotonic timestamps for testing."""
    return f"2026-04-27T{n:02d}:00:00Z"


# ---------------------------------------------------------------------------
# S1: Fresh hunch, no label
# ---------------------------------------------------------------------------

class TestS1:
    def test_fresh_hunch_unlabeled(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, "run01", "h-0001")
        assert r.label is None
        assert r.source == "unlabeled"


# ---------------------------------------------------------------------------
# S2: Fresh hunch, labeled tp
# ---------------------------------------------------------------------------

class TestS2:
    def test_labeled_tp(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(2),
                       category="confound", labeled_by="scientist_retro")
        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, "run01", "h-0001")
        assert r.label == "tp"
        assert r.source == "human"
        assert r.category == "confound"


# ---------------------------------------------------------------------------
# S3: Re-label tp → fp
# ---------------------------------------------------------------------------

class TestS3:
    def test_relabel_tp_to_fp(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(2))
        w.write_label("hb-0001", "run01", "h-0001", "fp", _ts(3))
        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, "run01", "h-0001")
        assert r.label == "fp"
        assert r.source == "human"


# ---------------------------------------------------------------------------
# S4: Label then retract
# ---------------------------------------------------------------------------

class TestS4:
    def test_label_then_retract(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(2))
        w.write_label("hb-0001", "run01", "h-0001", None, _ts(3))
        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, "run01", "h-0001")
        assert r.label is None
        assert r.source == "unlabeled"


# ---------------------------------------------------------------------------
# S5: Inherited label from another run
# ---------------------------------------------------------------------------

class TestS5:
    def test_inherited_label(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_link("hb-0001", "run02", "h-0005", _ts(2),
                      judge_score=0.87, source="ingest")
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(3))

        state = read_bank(_bank_path(tmp_path))

        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.label == "tp"
        assert r1.source == "human"

        r2 = resolve_label(state, "run02", "h-0005")
        assert r2.label == "tp"
        assert r2.source == "inherited"
        assert r2.inherited_from_run == "run01"
        assert r2.inherited_from_hunch_id == "h-0001"


# ---------------------------------------------------------------------------
# S6: Inherited label, human overrides
# ---------------------------------------------------------------------------

class TestS6:
    def test_inherited_label_human_overrides(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_link("hb-0001", "run02", "h-0005", _ts(2))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(3))
        w.write_label("hb-0001", "run02", "h-0005", "fp", _ts(4))

        state = read_bank(_bank_path(tmp_path))

        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.label == "tp"
        assert r1.source == "human"

        r2 = resolve_label(state, "run02", "h-0005")
        assert r2.label == "fp"
        assert r2.source == "human"


# ---------------------------------------------------------------------------
# S7: Label, then manually relink to different bank entry
# ---------------------------------------------------------------------------

class TestS7:
    def test_manual_relink_orphans_old_label(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell1", "desc1", "run01", "h-0001", _ts(1))
        w.write_entry("hb-0003", "smell3", "desc3", "run01", "h-0003", _ts(2))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(3))
        w.write_label("hb-0003", "run01", "h-0003", "fp", _ts(4))
        # Manual relink: h-0001 moves from hb-0001 to hb-0003
        w.write_link("hb-0003", "run01", "h-0001", _ts(5),
                      source="manual", replaces_bank_id="hb-0001")

        state = read_bank(_bank_path(tmp_path))

        # h-0001 now lives under hb-0003, inherits its fp label
        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.label == "fp"
        assert r1.source == "inherited"
        assert r1.inherited_from_run == "run01"
        assert r1.inherited_from_hunch_id == "h-0003"

        # h-0003 unchanged
        r3 = resolve_label(state, "run01", "h-0003")
        assert r3.label == "fp"
        assert r3.source == "human"


# ---------------------------------------------------------------------------
# S8: Manual relink, then undo
# ---------------------------------------------------------------------------

class TestS8:
    def test_manual_relink_then_undo(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell1", "desc1", "run01", "h-0001", _ts(1))
        w.write_entry("hb-0003", "smell3", "desc3", "run01", "h-0003", _ts(2))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(3))
        # Relink h-0001 to hb-0003
        w.write_link("hb-0003", "run01", "h-0001", _ts(4),
                      source="manual", replaces_bank_id="hb-0001")
        # Undo: relink h-0001 back to hb-0001
        w.write_link("hb-0001", "run01", "h-0001", _ts(5),
                      source="manual", replaces_bank_id="hb-0003")

        state = read_bank(_bank_path(tmp_path))

        r = resolve_label(state, "run01", "h-0001")
        assert r.label == "tp"
        assert r.source == "human"


# ---------------------------------------------------------------------------
# S9: Canonical label retracted, other hunches had inherited
# ---------------------------------------------------------------------------

class TestS9:
    def test_canonical_retracted_all_unlabeled(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_link("hb-0001", "run02", "h-0005", _ts(2))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(3))
        # Retract
        w.write_label("hb-0001", "run01", "h-0001", None, _ts(4))

        state = read_bank(_bank_path(tmp_path))

        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.label is None
        assert r1.source == "unlabeled"

        r2 = resolve_label(state, "run02", "h-0005")
        assert r2.label is None
        assert r2.source == "unlabeled"


# ---------------------------------------------------------------------------
# S10: Canonical retracted, but another linked hunch was also labeled
# ---------------------------------------------------------------------------

class TestS10:
    def test_canonical_retracted_other_label_promoted(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_link("hb-0001", "run02", "h-0005", _ts(2))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(3))
        w.write_label("hb-0001", "run02", "h-0005", "fp", _ts(4))
        # Retract the canonical label
        w.write_label("hb-0001", "run01", "h-0001", None, _ts(5))

        state = read_bank(_bank_path(tmp_path))

        # run01/h-0001: retracted locally, but run02's fp is now canonical → inherited
        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.label == "fp"
        assert r1.source == "inherited"
        assert r1.inherited_from_run == "run02"

        # run02/h-0005: still human fp
        r2 = resolve_label(state, "run02", "h-0005")
        assert r2.label == "fp"
        assert r2.source == "human"


# ---------------------------------------------------------------------------
# S11: Tombstoned run, canonical label survives
# ---------------------------------------------------------------------------

class TestS11:
    def test_tombstoned_run_label_survives(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(2))
        w.write_link("hb-0001", "run02", "h-0005", _ts(3))
        w.write_tombstone("run01", _ts(4))

        state = read_bank(_bank_path(tmp_path))

        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.source == "not_displayable"

        # run02 inherits the label even though the source run is tombstoned
        r2 = resolve_label(state, "run02", "h-0005")
        assert r2.label == "tp"
        assert r2.source == "inherited"
        assert r2.inherited_from_run == "run01"


# ---------------------------------------------------------------------------
# S12: Dormant bank entry revived by new run
# ---------------------------------------------------------------------------

class TestS12:
    def test_dormant_entry_revived(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(2))
        w.write_tombstone("run01", _ts(3))
        # New run rediscovers the same concern
        w.write_link("hb-0001", "run03", "h-0009", _ts(4), source="ingest")

        state = read_bank(_bank_path(tmp_path))

        r = resolve_label(state, "run03", "h-0009")
        assert r.label == "tp"
        assert r.source == "inherited"
        assert r.inherited_from_run == "run01"


# ---------------------------------------------------------------------------
# S13: Three runs, disagreement
# ---------------------------------------------------------------------------

class TestS13:
    def test_three_runs_disagreement(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_link("hb-0001", "run02", "h-0005", _ts(2))
        w.write_link("hb-0001", "run03", "h-0009", _ts(3))
        w.write_label("hb-0001", "run01", "h-0001", "tp", _ts(4))
        w.write_label("hb-0001", "run02", "h-0005", "fp", _ts(5))

        state = read_bank(_bank_path(tmp_path))

        r1 = resolve_label(state, "run01", "h-0001")
        assert r1.label == "tp"
        assert r1.source == "human"

        r2 = resolve_label(state, "run02", "h-0005")
        assert r2.label == "fp"
        assert r2.source == "human"

        # run03 inherits canonical (earliest-labeled = run01's tp)
        r3 = resolve_label(state, "run03", "h-0009")
        assert r3.label == "tp"
        assert r3.source == "inherited"
        assert r3.inherited_from_run == "run01"


# ---------------------------------------------------------------------------
# S14: Live feedback label overridden by evaluator
# ---------------------------------------------------------------------------

class TestS14:
    def test_evaluator_outranks_live_feedback(self, tmp_path):
        """Tier 1 (scientist_retro) outranks tier 2 (operational_live)
        regardless of timestamp order."""
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", ":live", "h-0001", _ts(1))
        # Scientist presses "good" in TUI → operational_live tp
        w.write_label("hb-0001", ":live", "h-0001", "tp", _ts(2),
                       labeled_by="operational_live")
        # Evaluator labels fp later
        w.write_label("hb-0001", ":live", "h-0001", "fp", _ts(3),
                       labeled_by="scientist_retro")

        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, ":live", "h-0001")
        assert r.label == "fp"
        assert r.source == "human"

    def test_evaluator_outranks_live_feedback_reverse_order(self, tmp_path):
        """Tier 1 wins even when it has an earlier timestamp than tier 2."""
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", ":live", "h-0001", _ts(1))
        # Evaluator labels fp first
        w.write_label("hb-0001", ":live", "h-0001", "fp", _ts(2),
                       labeled_by="scientist_retro")
        # Scientist later presses "good" in TUI
        w.write_label("hb-0001", ":live", "h-0001", "tp", _ts(3),
                       labeled_by="operational_live")

        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, ":live", "h-0001")
        assert r.label == "fp"
        assert r.source == "human"


# ---------------------------------------------------------------------------
# S15: Inherited tier ranking
# ---------------------------------------------------------------------------

class TestS15:
    def test_inherited_prefers_evaluator_over_feedback(self, tmp_path):
        """For inheritance, tier 1 labels are preferred even if a tier 2
        label was first by timestamp."""
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", ":live", "h-0001", _ts(1))
        w.write_link("hb-0001", "run02", "h-0005", _ts(2))
        w.write_link("hb-0001", "run03", "h-0009", _ts(3))
        # Live feedback first (tier 2, earlier ts)
        w.write_label("hb-0001", ":live", "h-0001", "tp", _ts(4),
                       labeled_by="operational_live")
        # Evaluator labels run02 later (tier 1, later ts)
        w.write_label("hb-0001", "run02", "h-0005", "fp", _ts(5),
                       labeled_by="scientist_retro")

        state = read_bank(_bank_path(tmp_path))

        # :live/h-0001 has a local label (operational_live tp)
        r1 = resolve_label(state, ":live", "h-0001")
        assert r1.label == "tp"
        assert r1.source == "human"

        # run02/h-0005 has a local label (scientist_retro fp)
        r2 = resolve_label(state, "run02", "h-0005")
        assert r2.label == "fp"
        assert r2.source == "human"

        # run03/h-0009: no local label, inherits tier 1 (run02's fp)
        # even though :live's tp was earlier
        r3 = resolve_label(state, "run03", "h-0009")
        assert r3.label == "fp"
        assert r3.source == "inherited"
        assert r3.inherited_from_run == "run02"

    def test_inherited_falls_back_to_feedback_when_no_evaluator(self, tmp_path):
        """If only tier 2 labels exist, they are used for inheritance."""
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", ":live", "h-0001", _ts(1))
        w.write_link("hb-0001", "run02", "h-0005", _ts(2))
        # Only live feedback, no evaluator labels
        w.write_label("hb-0001", ":live", "h-0001", "tp", _ts(3),
                       labeled_by="operational_live")

        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, "run02", "h-0005")
        assert r.label == "tp"
        assert r.source == "inherited"
        assert r.inherited_from_run == ":live"


# ---------------------------------------------------------------------------
# Writer guards
# ---------------------------------------------------------------------------

class TestWriterGuards:
    def test_id_allocation_monotonic(self, tmp_path):
        w = _writer(tmp_path)
        assert w.allocate_id() == "hb-0001"
        assert w.allocate_id() == "hb-0002"
        assert w.allocate_id() == "hb-0003"

    def test_id_allocation_resumes_from_disk(self, tmp_path):
        w1 = _writer(tmp_path)
        w1.write_entry("hb-0001", "s", "d", "r", "h-0001", _ts(1))
        w1.write_entry("hb-0002", "s", "d", "r", "h-0002", _ts(2))
        # New writer picks up from disk
        w2 = BankWriter(_bank_path(tmp_path))
        assert w2.allocate_id() == "hb-0003"

    def test_ts_monotonicity_enforced(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "s", "d", "r", "h-0001", _ts(2))
        with pytest.raises(RuntimeError, match="monotonicity"):
            w.write_entry("hb-0002", "s", "d", "r", "h-0002", _ts(1))

    def test_ts_equal_rejected(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "s", "d", "r", "h-0001", _ts(1))
        with pytest.raises(RuntimeError, match="monotonicity"):
            w.write_entry("hb-0002", "s", "d", "r", "h-0002", _ts(1))

    def test_ts_monotonicity_resumes_from_disk(self, tmp_path):
        w1 = _writer(tmp_path)
        w1.write_entry("hb-0001", "s", "d", "r", "h-0001", _ts(5))
        # New writer picks up last ts from disk
        w2 = BankWriter(_bank_path(tmp_path))
        with pytest.raises(RuntimeError, match="monotonicity"):
            w2.write_entry("hb-0002", "s", "d", "r", "h-0002", _ts(3))
        # But a later ts works
        w2.write_entry("hb-0002", "s", "d", "r", "h-0002", _ts(6))


# ---------------------------------------------------------------------------
# Reader edge cases
# ---------------------------------------------------------------------------

class TestReaderEdgeCases:
    def test_empty_bank(self, tmp_path):
        state = read_bank(_bank_path(tmp_path))
        assert state.entries == {}
        assert state.tombstoned_runs == set()
        assert state.hunch_to_bank == {}

    def test_unknown_hunch_resolves_unlabeled(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "s", "d", "run01", "h-0001", _ts(1))
        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, "run99", "h-9999")
        assert r.label is None
        assert r.source == "unlabeled"

    def test_link_to_nonexistent_entry_ignored(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "s", "d", "run01", "h-0001", _ts(1))
        w.write_link("hb-9999", "run02", "h-0005", _ts(2))
        state = read_bank(_bank_path(tmp_path))
        assert ("run02", "h-0005") not in state.hunch_to_bank

    def test_label_on_nonexistent_entry_ignored(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "s", "d", "run01", "h-0001", _ts(1))
        w.write_label("hb-9999", "run01", "h-0001", "tp", _ts(2))
        state = read_bank(_bank_path(tmp_path))
        entry = state.entries["hb-0001"]
        assert len(entry.labels) == 0


# ---------------------------------------------------------------------------
# S11 variant: tombstoned source but label from tombstoned run still
# propagates (labels are facts, not run-validity judgments)
# ---------------------------------------------------------------------------

class TestTombstoneEdgeCases:
    def test_tombstoned_run_not_displayable(self, tmp_path):
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "s", "d", "run01", "h-0001", _ts(1))
        w.write_tombstone("run01", _ts(2))
        state = read_bank(_bank_path(tmp_path))
        r = resolve_label(state, "run01", "h-0001")
        assert r.source == "not_displayable"

    def test_dormant_entry_still_in_bank(self, tmp_path):
        """Dormant entries (all links tombstoned) remain for dedup matching."""
        w = _writer(tmp_path)
        w.write_entry("hb-0001", "smell", "desc", "run01", "h-0001", _ts(1))
        w.write_tombstone("run01", _ts(2))
        state = read_bank(_bank_path(tmp_path))
        assert "hb-0001" in state.entries
        entry = state.entries["hb-0001"]
        assert entry.canonical_smell == "smell"
