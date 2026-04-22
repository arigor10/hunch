"""Tests for hunch.journal — hunches.jsonl + feedback.jsonl.

Covers:
  - HunchesWriter allocates monotonic ids, scans existing ids on restart
  - emit + status_change events are append-only and well-formed
  - read_current_hunches folds events to the right current status
  - duplicate emit is a no-op (matches framework dedup contract)
  - status_change for an unknown hunch_id is skipped, not fatal
  - unknown event types are skipped (forward-compat)
  - FeedbackWriter enforces the explicit-label closed set
  - FeedbackWriter accepts implicit replies verbatim
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunch.critic import Hunch, TriggeringRefs
from hunch.journal import (
    FeedbackWriter,
    HunchesWriter,
    read_current_hunches,
)


def _read_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _mk_hunch(smell: str = "x", description: str = "y") -> Hunch:
    return Hunch(
        smell=smell,
        description=description,
        triggering_refs=TriggeringRefs(chunks=["c-1"], artifacts=[]),
    )


# ---------------------------------------------------------------------------
# HunchesWriter — allocation + emit
# ---------------------------------------------------------------------------

def test_allocate_id_is_monotonic_from_empty_file(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "h.jsonl")
    assert w.allocate_id() == "h-0001"
    assert w.allocate_id() == "h-0002"
    assert w.allocate_id() == "h-0003"


def test_allocate_id_resumes_from_existing_file(tmp_path):
    # Simulate a pre-existing file with h-0007 already emitted.
    hp = tmp_path / "h.jsonl"
    with open(hp, "w") as f:
        f.write(json.dumps({
            "type": "emit",
            "hunch_id": "h-0007",
            "ts": "t1",
            "emitted_by_tick": 1,
            "smell": "x", "description": "y",
            "triggering_refs": {"chunks": [], "artifacts": []},
        }) + "\n")
    w = HunchesWriter(hunches_path=hp)
    assert w.allocate_id() == "h-0008"


def test_write_emit_produces_appendix_shape(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "h.jsonl")
    hid = w.allocate_id()
    w.write_emit(
        _mk_hunch("smell-A", "desc-A"), hid, "t1",
        emitted_by_tick=42, bookmark_prev=5, bookmark_now=17,
    )
    entries = [e for e in _read_jsonl(w.hunches_path) if e.get("type") != "meta"]
    assert len(entries) == 1
    e = entries[0]
    assert e["type"] == "emit"
    assert e["hunch_id"] == hid
    assert e["ts"] == "t1"
    assert e["emitted_by_tick"] == 42
    assert e["bookmark_prev"] == 5
    assert e["bookmark_now"] == 17
    assert e["smell"] == "smell-A"
    assert e["description"] == "desc-A"
    assert e["triggering_refs"] == {"chunks": ["c-1"], "artifacts": []}


# ---------------------------------------------------------------------------
# HunchesWriter — status_change + append-only
# ---------------------------------------------------------------------------

def test_status_change_appends_not_mutates(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "h.jsonl")
    hid = w.allocate_id()
    w.write_emit(_mk_hunch(), hid, "t1", emitted_by_tick=1, bookmark_prev=0, bookmark_now=1)
    w.write_status_change(hid, "shown_to_researcher", "t2", by="hook:ups")
    w.write_status_change(hid, "suppressed", "t3", by="scientist_key:alt_s")
    entries = [e for e in _read_jsonl(w.hunches_path) if e.get("type") != "meta"]
    assert len(entries) == 3  # all three events present, nothing rewritten
    assert [e["type"] for e in entries] == ["emit", "status_change", "status_change"]
    assert entries[1]["new_status"] == "shown_to_researcher"
    assert entries[2]["new_status"] == "suppressed"


# ---------------------------------------------------------------------------
# read_current_hunches — folding
# ---------------------------------------------------------------------------

def test_read_current_hunches_empty_file(tmp_path):
    assert read_current_hunches(tmp_path / "missing.jsonl") == []


def test_read_current_hunches_folds_to_latest_status(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "h.jsonl")
    h1 = w.allocate_id()
    h2 = w.allocate_id()
    w.write_emit(_mk_hunch("a", "aa"), h1, "t1", emitted_by_tick=1, bookmark_prev=0, bookmark_now=3)
    w.write_emit(_mk_hunch("b", "bb"), h2, "t2", emitted_by_tick=2, bookmark_prev=3, bookmark_now=11)
    w.write_status_change(h1, "shown_to_researcher", "t3", by="hook:ups")
    w.write_status_change(h1, "suppressed", "t4", by="scientist:alt_s")

    records = read_current_hunches(w.hunches_path)
    assert [r.hunch_id for r in records] == [h1, h2]

    r1 = records[0]
    assert r1.smell == "a"
    assert r1.status == "suppressed"  # latest status
    assert r1.bookmark_prev == 0
    assert r1.bookmark_now == 3
    assert [h["new_status"] for h in r1.history] == [
        "shown_to_researcher",
        "suppressed",
    ]

    r2 = records[1]
    assert r2.status == "pending"  # no status_change applied
    assert r2.bookmark_prev == 3
    assert r2.bookmark_now == 11
    assert r2.history == []


def test_read_current_hunches_skips_duplicate_emit(tmp_path):
    # Two emits with the same hunch_id: framework dedup contract says
    # keep the first. We write the duplicate manually since
    # HunchesWriter.allocate_id won't produce one.
    hp = tmp_path / "h.jsonl"
    lines = [
        {"type": "emit", "hunch_id": "h-0001", "ts": "t1",
         "emitted_by_tick": 1, "smell": "first", "description": "d1",
         "triggering_refs": {"chunks": [], "artifacts": []}},
        {"type": "emit", "hunch_id": "h-0001", "ts": "t2",
         "emitted_by_tick": 2, "smell": "second", "description": "d2",
         "triggering_refs": {"chunks": [], "artifacts": []}},
    ]
    with open(hp, "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    records = read_current_hunches(hp)
    assert len(records) == 1
    assert records[0].smell == "first"


def test_read_current_hunches_skips_status_change_for_unknown_id(tmp_path):
    hp = tmp_path / "h.jsonl"
    lines = [
        {"type": "status_change", "hunch_id": "h-9999",
         "ts": "t1", "new_status": "suppressed", "by": "?"},
        {"type": "emit", "hunch_id": "h-0001", "ts": "t2",
         "emitted_by_tick": 1, "smell": "x", "description": "y",
         "triggering_refs": {"chunks": [], "artifacts": []}},
    ]
    with open(hp, "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    records = read_current_hunches(hp)
    assert len(records) == 1
    assert records[0].status == "pending"


def test_read_current_hunches_tolerates_unknown_event_type(tmp_path):
    hp = tmp_path / "h.jsonl"
    lines = [
        {"type": "emit", "hunch_id": "h-0001", "ts": "t1",
         "emitted_by_tick": 1, "smell": "x", "description": "y",
         "triggering_refs": {"chunks": [], "artifacts": []}},
        {"type": "future_event", "hunch_id": "h-0001",
         "ts": "t2", "payload": "whatever"},
    ]
    with open(hp, "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    records = read_current_hunches(hp)
    assert len(records) == 1
    assert records[0].status == "pending"


def test_read_current_hunches_treats_shrinking_bookmark_window_as_unknown(tmp_path):
    # A corrupted / hand-edited emit with bookmark_now < bookmark_prev
    # must not be served verbatim (offline evaluators would pull a
    # nonsensical slice). Fall back to "unknown".
    hp = tmp_path / "h.jsonl"
    lines = [
        {"type": "emit", "hunch_id": "h-0001", "ts": "t1",
         "emitted_by_tick": 1, "bookmark_prev": 17, "bookmark_now": 5,
         "smell": "x", "description": "y",
         "triggering_refs": {"chunks": [], "artifacts": []}},
    ]
    with open(hp, "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    records = read_current_hunches(hp)
    assert len(records) == 1
    assert records[0].bookmark_prev == -1
    assert records[0].bookmark_now == -1


def test_read_current_hunches_tolerates_legacy_emit_without_bookmarks(tmp_path):
    # Emit records written before bookmark_prev/bookmark_now were
    # introduced must still load; readers default to -1 so "unknown"
    # is distinguishable from a real 0.
    hp = tmp_path / "h.jsonl"
    lines = [
        {"type": "emit", "hunch_id": "h-0001", "ts": "t1",
         "emitted_by_tick": 1, "smell": "x", "description": "y",
         "triggering_refs": {"chunks": [], "artifacts": []}},
    ]
    with open(hp, "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    records = read_current_hunches(hp)
    assert len(records) == 1
    assert records[0].bookmark_prev == -1
    assert records[0].bookmark_now == -1


def test_read_current_hunches_skips_malformed_lines(tmp_path):
    hp = tmp_path / "h.jsonl"
    with open(hp, "w") as f:
        f.write("not-json\n")
        f.write(json.dumps({
            "type": "emit", "hunch_id": "h-0001", "ts": "t1",
            "emitted_by_tick": 1, "smell": "x", "description": "y",
            "triggering_refs": {"chunks": [], "artifacts": []},
        }) + "\n")
        f.write("{broken json\n")
    records = read_current_hunches(hp)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# FeedbackWriter
# ---------------------------------------------------------------------------

def test_feedback_writer_explicit_labels(tmp_path):
    fw = FeedbackWriter(feedback_path=tmp_path / "fb.jsonl")
    fw.write_explicit("h-0001", "good", "t1")
    fw.write_explicit("h-0001", "skip", "t2")
    fw.write_explicit("h-0002", "bad", "t3")
    entries = _read_jsonl(fw.feedback_path)
    assert len(entries) == 3
    assert [e["label"] for e in entries] == ["good", "skip", "bad"]
    assert all(e["channel"] == "explicit" for e in entries)
    assert all(e["scientist_reply"] is None for e in entries)


def test_feedback_writer_rejects_unknown_explicit_label(tmp_path):
    fw = FeedbackWriter(feedback_path=tmp_path / "fb.jsonl")
    with pytest.raises(ValueError, match="good\\|bad\\|skip"):
        fw.write_explicit("h-0001", "meh", "t1")


def test_feedback_writer_implicit_preserves_reply_text(tmp_path):
    fw = FeedbackWriter(feedback_path=tmp_path / "fb.jsonl")
    reply = "huh, good point — I hadn't considered that"
    fw.write_implicit("h-0001", reply, "t1")
    entries = _read_jsonl(fw.feedback_path)
    assert entries[0]["channel"] == "implicit"
    assert entries[0]["label"] == "implicit"
    assert entries[0]["scientist_reply"] == reply
