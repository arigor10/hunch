"""Tests for the Critic protocol (value shapes + stub implementation).

The real (Sonnet-backed) Critic has its own tests; this file covers:
  - Hunch / TriggeringRefs round-trip to/from dict
  - The `hunches.jsonl` emit-record shape matches framework_v0.md Appendix A
  - StubCritic enforces the init/tick/shutdown protocol invariants
"""

from __future__ import annotations

import pytest

from hunch.critic import (
    Hunch,
    StubCritic,
    TriggeringRefs,
    hunch_emit_record,
)


# ---------------------------------------------------------------------------
# Value shapes
# ---------------------------------------------------------------------------

def test_triggering_refs_round_trip():
    refs = TriggeringRefs(chunks=["c-1", "c-2"], artifacts=["notes.md"])
    d = refs.to_dict()
    assert d == {"chunks": ["c-1", "c-2"], "artifacts": ["notes.md"]}
    back = TriggeringRefs.from_dict(d)
    assert back == refs


def test_triggering_refs_from_empty_dict_defaults_to_empty_lists():
    refs = TriggeringRefs.from_dict({})
    assert refs.chunks == []
    assert refs.artifacts == []


def test_hunch_round_trip():
    h = Hunch(
        smell="3x R2 discrepancy between calibration runs",
        description="Yesterday reported 0.3-0.5, today reports 0.94. No method change.",
        triggering_refs=TriggeringRefs(
            chunks=["c-0031", "c-0042"],
            artifacts=["writeups/exp_042.md"],
        ),
    )
    d = h.to_dict()
    assert d["smell"] == h.smell
    assert d["description"] == h.description
    assert d["triggering_refs"] == {
        "chunks": ["c-0031", "c-0042"],
        "artifacts": ["writeups/exp_042.md"],
    }
    assert Hunch.from_dict(d) == h


def test_hunch_emit_record_matches_appendix_shape():
    h = Hunch(
        smell="Seed labeled fixed but 3 runs give different numbers.",
        description="Run logs at chunks c-0040/41/42 show distinct final metrics despite seed=42.",
        triggering_refs=TriggeringRefs(
            chunks=["c-0040", "c-0041", "c-0042"],
            artifacts=[],
        ),
    )
    rec = hunch_emit_record(
        h,
        hunch_id="h-0007",
        ts="2026-04-14T10:23:15Z",
        emitted_by_tick=87,
        bookmark_prev=150,
        bookmark_now=164,
    )
    # Shape per framework_v0.md Appendix A
    assert rec["type"] == "emit"
    assert rec["hunch_id"] == "h-0007"
    assert rec["ts"] == "2026-04-14T10:23:15Z"
    assert rec["emitted_by_tick"] == 87
    assert rec["bookmark_prev"] == 150
    assert rec["bookmark_now"] == 164
    assert rec["smell"] == h.smell
    assert rec["description"] == h.description
    assert rec["triggering_refs"] == {
        "chunks": ["c-0040", "c-0041", "c-0042"],
        "artifacts": [],
    }
    # Deliberately absent per critic_v0.md §Output schema
    assert "diagnostic" not in rec
    assert "confidence" not in rec
    assert "who" not in rec


def test_hunch_emit_record_rejects_shrinking_bookmark_window():
    # The replay buffer's tick_seq is strictly monotonic, so a tick's
    # window can never shrink. If the framework ever hands us
    # bookmark_now < bookmark_prev that's a wiring bug we want to
    # catch at write time — not discover later from a corrupted journal.
    h = Hunch(smell="s", description="d")
    with pytest.raises(ValueError, match="bookmark_now"):
        hunch_emit_record(
            h, hunch_id="h-0001", ts="t1", emitted_by_tick=1,
            bookmark_prev=17, bookmark_now=5,
        )


# ---------------------------------------------------------------------------
# StubCritic
# ---------------------------------------------------------------------------

def test_stub_critic_happy_path():
    c = StubCritic()
    c.init({"model": "stub"})
    assert c.initialized
    assert c.config == {"model": "stub"}

    hunches = c.tick(tick_id="t-1", bookmark_prev=0, bookmark_now=10)
    assert hunches == []
    assert c.tick_log == [
        {"tick_id": "t-1", "bookmark_prev": 0, "bookmark_now": 10}
    ]

    c.shutdown()
    assert c.shutdown_called


def test_stub_critic_rejects_tick_before_init():
    c = StubCritic()
    with pytest.raises(RuntimeError, match="before init"):
        c.tick("t-1", 0, 0)


def test_stub_critic_rejects_tick_after_shutdown():
    c = StubCritic()
    c.init({})
    c.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        c.tick("t-1", 0, 0)


def test_stub_critic_rejects_double_init():
    c = StubCritic()
    c.init({})
    with pytest.raises(RuntimeError, match="init called twice"):
        c.init({})


def test_stub_critic_rejects_non_monotonic_bookmark():
    c = StubCritic()
    c.init({})
    c.tick("t-1", 0, 10)
    # Walking the bookmark backwards is a framework wiring bug — catch it.
    with pytest.raises(ValueError, match="bookmark_now"):
        c.tick("t-2", 10, 5)


def test_stub_critic_accepts_equal_bookmarks_noop_tick():
    """If nothing new happened, bookmark_now == bookmark_prev is valid
    (the framework still pinged the Critic; it just has nothing to do)."""
    c = StubCritic()
    c.init({})
    assert c.tick("t-1", 10, 10) == []
