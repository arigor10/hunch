"""Tests for the accumulating Critic prompt stream.

Covers: append ordering, timeline rendering, open-hunch survival across
purge, living-artifact snapshot reconstruction, token bookkeeping
round-trip, purge boundary math (low/high watermarks).
"""

from __future__ import annotations

import pytest

from hunch.critic.accumulator import (
    ArtifactEditEvent,
    ArtifactWriteEvent,
    ChunkTextEvent,
    CriticPromptStream,
    InlineHunchEvent,
    LabelEvent,
    chunk_id_for_seq,
    load_prompt_template,
)
from hunch.critic.protocol import Hunch, TriggeringRefs


# ---------------------------------------------------------------------------
# Basic chunk-id / event identity
# ---------------------------------------------------------------------------

def test_chunk_id_for_seq_zero_padded():
    assert chunk_id_for_seq(0) == "c-0000"
    assert chunk_id_for_seq(7) == "c-0007"
    assert chunk_id_for_seq(1234) == "c-1234"


# ---------------------------------------------------------------------------
# Append + render
# ---------------------------------------------------------------------------

def _stream() -> CriticPromptStream:
    return CriticPromptStream(preamble="SYSTEM")


def test_empty_render_shows_placeholders():
    s = _stream()
    out = s.render()
    assert "SYSTEM" in out
    assert "(no open hunches carried over)" in out
    assert "(no .md artifacts at the purge boundary)" in out
    assert "(timeline is empty)" in out


def test_append_chunk_text_renders_with_chunk_id_and_role():
    s = _stream()
    s.append_chunk_text(tick_seq=5, role="assistant", text="running exp A")
    out = s.render()
    assert "[c-0005] (assistant) running exp A" in out


def test_append_artifact_write_includes_full_content_inline():
    s = _stream()
    s.append_artifact_write(tick_seq=3, path="writeups/exp.md", content="# hi\n\nbody")
    out = s.render()
    assert "[c-0003] (artifact-write) writeups/exp.md" in out
    assert "# hi\n\nbody" in out


def test_append_artifact_edit_includes_old_and_new():
    s = _stream()
    s.append_artifact_edit(
        tick_seq=4,
        path="writeups/exp.md",
        old_string="R²=0.3",
        new_string="R²=0.94",
    )
    out = s.render()
    assert "[c-0004] (artifact-edit) writeups/exp.md" in out
    assert "R²=0.3" in out
    assert "R²=0.94" in out


def test_append_hunch_includes_refs_inline():
    s = _stream()
    h = Hunch(
        smell="R² disagrees",
        description="yesterday 0.3, today 0.94.",
        triggering_refs=TriggeringRefs(
            chunks=["c-0001"], artifacts=["writeups/exp.md"]
        ),
    )
    s.append_hunch(tick_seq=10, hunch_id="h-0001", hunch=h)
    out = s.render()
    assert "[c-0010] (critic-hunch h-0001) R² disagrees" in out
    assert "yesterday 0.3, today 0.94." in out
    assert "chunks: c-0001" in out
    assert "artifacts: writeups/exp.md" in out


def test_append_label_renders_contemporaneously():
    s = _stream()
    h = Hunch(smell="s", description="d")
    s.append_hunch(tick_seq=10, hunch_id="h-0001", hunch=h)
    s.append_chunk_text(tick_seq=11, role="user", text="interesting")
    s.append_label(tick_seq=12, hunch_id="h-0001", label="good")
    out = s.render()
    # Label appears in timeline (not in a trailing epilogue).
    assert "[c-0012] (scientist-label h-0001) good" in out


# ---------------------------------------------------------------------------
# Current-artifact tracking (used at purge time)
# ---------------------------------------------------------------------------

def test_write_then_edit_tracks_current_content():
    s = _stream()
    s.append_artifact_write(tick_seq=1, path="a.md", content="R²=0.3 and notes")
    s.append_artifact_edit(
        tick_seq=2,
        path="a.md",
        old_string="R²=0.3",
        new_string="R²=0.94",
    )
    assert s._current_artifacts["a.md"] == "R²=0.94 and notes"


def test_edit_before_write_leaves_current_artifacts_empty():
    s = _stream()
    s.append_artifact_edit(
        tick_seq=1, path="a.md", old_string="x", new_string="y"
    )
    assert "a.md" not in s._current_artifacts


def test_edit_applied_on_top_of_living_artifacts_post_purge():
    """After purge the original write lives in living_artifacts; a
    subsequent edit should still produce a correct current snapshot."""
    s = CriticPromptStream(preamble="P", low_watermark=400, high_watermark=600)
    s.append_artifact_write(tick_seq=1, path="a.md", content="base text X here")
    # Fabricate the post-purge state: living_artifacts holds the write,
    # _current_artifacts matches, timeline trimmed.
    s.living_artifacts = dict(s._current_artifacts)
    s._current_artifacts = dict(s.living_artifacts)
    s.timeline = []
    s._event_tokens = []  # stays aligned with the fabricated empty timeline
    # Now an edit arrives post-purge.
    s.append_artifact_edit(tick_seq=10, path="a.md", old_string="X", new_string="Y")
    assert s._current_artifacts["a.md"] == "base text Y here"


def test_edit_with_missing_old_string_is_noop_on_current_state():
    s = _stream()
    s.append_artifact_write(tick_seq=1, path="a.md", content="hello")
    s.append_artifact_edit(
        tick_seq=2, path="a.md", old_string="MISSING", new_string="x"
    )
    assert s._current_artifacts["a.md"] == "hello"


def test_multiple_writes_to_same_path_keeps_latest():
    s = _stream()
    s.append_artifact_write(tick_seq=1, path="a.md", content="v1")
    s.append_artifact_write(tick_seq=5, path="a.md", content="v2")
    assert s._current_artifacts["a.md"] == "v2"


# ---------------------------------------------------------------------------
# Open-hunch collection
# ---------------------------------------------------------------------------

def test_open_hunches_excludes_labeled_ones():
    s = _stream()
    s.append_hunch(1, "h-0001", Hunch(smell="a", description="d"))
    s.append_hunch(2, "h-0002", Hunch(smell="b", description="d"))
    s.append_label(3, hunch_id="h-0001", label="good")
    open_ = s._collect_open_hunches()
    assert [h.hunch_id for h in open_] == ["h-0002"]


def test_open_hunches_spans_surviving_and_timeline():
    s = _stream()
    s.surviving_hunches = [
        InlineHunchEvent(
            tick_seq=0, hunch_id="h-0001", smell="old", description="d"
        )
    ]
    s.append_hunch(10, "h-0002", Hunch(smell="new", description="d"))
    open_ = s._collect_open_hunches()
    assert [h.hunch_id for h in open_] == ["h-0001", "h-0002"]


def test_open_hunches_dedupes_surviving_vs_timeline_copy():
    s = _stream()
    s.surviving_hunches = [
        InlineHunchEvent(
            tick_seq=0, hunch_id="h-0001", smell="x", description="d"
        )
    ]
    # An InlineHunchEvent with the same id in the timeline — surviving
    # wins (it was emitted first).
    s.append_hunch(10, "h-0001", Hunch(smell="x2", description="d2"))
    open_ = s._collect_open_hunches()
    assert len(open_) == 1
    assert open_[0].smell == "x"


# ---------------------------------------------------------------------------
# Purge math + survival
# ---------------------------------------------------------------------------

def _make_small_stream() -> CriticPromptStream:
    """Small watermarks so we can exercise purge without gigantic fixtures.

    Budget has to clear the fixed-region overhead (~80 tokens for
    preamble + placeholder blocks + headers). Low = 400, high = 600
    leaves room for several kept events.
    """
    return CriticPromptStream(
        preamble="P",
        low_watermark=400,
        high_watermark=600,
        chars_per_token=3.5,
    )


def test_should_purge_returns_false_when_empty():
    s = _make_small_stream()
    assert not s.should_purge()


def test_should_purge_trips_when_timeline_big_enough():
    s = _make_small_stream()
    # Stuff the timeline with chatter until projected_tokens ≥ high wm.
    for i in range(100):
        s.append_chunk_text(i, "user", "x" * 40)
    assert s.should_purge()


def test_purge_drops_oldest_events_first():
    s = _make_small_stream()
    for i in range(100):
        s.append_chunk_text(i, "user", "x" * 40)
    assert s.should_purge()
    dropped = s.purge()
    assert dropped > 0
    # Remaining timeline should be the newest events.
    remaining_ticks = [
        e.tick_seq for e in s.timeline if isinstance(e, ChunkTextEvent)
    ]
    # Monotonic increase preserved.
    assert remaining_ticks == sorted(remaining_ticks)
    # Last one is still 99 (newest).
    assert remaining_ticks[-1] == 99


def test_purge_moves_open_hunch_from_dropped_into_surviving():
    s = _make_small_stream()
    # Tick 0: emit a hunch that will be labeled nowhere.
    s.append_hunch(0, "h-0001", Hunch(smell="old open", description="d"))
    # Flood timeline so purge drops the first event.
    for i in range(1, 100):
        s.append_chunk_text(i, "user", "y" * 40)
    assert s.should_purge()
    s.purge()
    assert any(h.hunch_id == "h-0001" for h in s.surviving_hunches)
    # It should no longer be present in the trimmed timeline.
    assert not any(
        isinstance(e, InlineHunchEvent) and e.hunch_id == "h-0001"
        for e in s.timeline
    )


def test_purge_does_not_duplicate_hunch_already_in_kept_timeline():
    s = CriticPromptStream(
        preamble="P", low_watermark=400, high_watermark=600, chars_per_token=3.5
    )
    for i in range(50):
        s.append_chunk_text(i, "user", "a" * 60)
    # A hunch near the end — should stay inline in kept timeline.
    s.append_hunch(
        51, "h-late", Hunch(smell="recent", description="still open")
    )
    for i in range(52, 200):
        s.append_chunk_text(i, "user", "b" * 60)
    assert s.should_purge()
    s.purge()
    kept_ids = {
        e.hunch_id for e in s.timeline if isinstance(e, InlineHunchEvent)
    }
    # If h-late is still inline in kept, it must NOT also be in
    # surviving_hunches (no dupes).
    if "h-late" in kept_ids:
        assert not any(h.hunch_id == "h-late" for h in s.surviving_hunches)


def test_purge_drops_labeled_hunch_entirely():
    s = _make_small_stream()
    s.append_hunch(0, "h-0001", Hunch(smell="old", description="d"))
    s.append_label(1, hunch_id="h-0001", label="bad")
    for i in range(2, 100):
        s.append_chunk_text(i, "user", "z" * 40)
    assert s.should_purge()
    s.purge()
    assert not any(h.hunch_id == "h-0001" for h in s.surviving_hunches)


def test_purge_evicts_oldest_touched_artifact_over_budget():
    """When _current_artifacts exceeds artifact_budget_tokens, purge
    keeps newest-touched files and drops older ones."""
    s = CriticPromptStream(
        preamble="P",
        low_watermark=1000,
        high_watermark=1500,
        chars_per_token=3.5,
        artifact_budget_tokens=50,  # very tight: ~175 char budget
    )
    s.append_artifact_write(1, "old.md", "X" * 100)    # touched at 1
    s.append_artifact_write(2, "mid.md", "Y" * 100)    # touched at 2
    s.append_artifact_write(3, "new.md", "Z" * 100)    # touched at 3
    # Also some chatter to push timeline past high watermark.
    for i in range(4, 200):
        s.append_chunk_text(i, "user", "blah" * 30)
    s.purge()
    # Only newest fits. mid/old should be evicted.
    assert "new.md" in s.living_artifacts
    assert "old.md" not in s.living_artifacts


def test_purge_keeps_all_artifacts_when_under_budget():
    s = CriticPromptStream(
        preamble="P",
        low_watermark=1000,
        high_watermark=1500,
        chars_per_token=3.5,
        artifact_budget_tokens=1000,  # generous
    )
    s.append_artifact_write(1, "a.md", "small")
    s.append_artifact_write(2, "b.md", "small")
    for i in range(3, 200):
        s.append_chunk_text(i, "user", "blah" * 30)
    s.purge()
    assert set(s.living_artifacts) == {"a.md", "b.md"}


def test_edit_updates_last_touched_for_eviction():
    """An edit to an older file bumps it to 'newest' for eviction order."""
    s = CriticPromptStream(
        preamble="P",
        low_watermark=1000,
        high_watermark=1500,
        chars_per_token=3.5,
        artifact_budget_tokens=50,
    )
    s.append_artifact_write(1, "old.md", "AAABBB")
    s.append_artifact_write(2, "new.md", "CCCDDD")
    # Re-touch old.md via edit at tick 10 — now it's the newest.
    s.append_artifact_edit(10, "old.md", old_string="AAA", new_string="EEE")
    for i in range(11, 200):
        s.append_chunk_text(i, "user", "yakyak" * 30)
    s.purge()
    # Budget only fits one — the more-recently-touched "old.md" wins.
    assert "old.md" in s.living_artifacts
    assert s.living_artifacts["old.md"].startswith("EEE")


def test_purge_builds_living_artifacts_from_current_content():
    s = _make_small_stream()
    s.append_artifact_write(0, "a.md", "v1-content")
    s.append_artifact_edit(
        1, "a.md", old_string="v1", new_string="v2"
    )
    # Flood to force purge.
    for i in range(2, 200):
        s.append_chunk_text(i, "user", "q" * 40)
    assert s.should_purge()
    s.purge()
    assert s.living_artifacts == {"a.md": "v2-content"}


def test_purge_synthesizes_observation_bookkeeping():
    """Post-purge, we re-anchor from the fresh fixed-region estimate plus
    the observed per-event tokens of the kept timeline. The next real
    observation corrects the fixed-region part."""
    s = _make_small_stream()
    for i in range(100):
        s.append_chunk_text(i, "user", "x" * 40)
    s.update_observed_tokens(800)
    assert s._observed_prefix_tokens == 800
    s.purge()
    # Observation synthesized (not reset to None) — anchored on kept data.
    assert s._observed_prefix_tokens is not None
    assert s._observed_timeline_len == len(s.timeline)
    # Synthesized value should be at or below the low watermark (that's
    # what purge targets).
    assert s._observed_prefix_tokens <= s.low_watermark + 50


def test_purge_returns_zero_when_timeline_fits():
    s = _make_small_stream()
    s.append_chunk_text(0, "user", "small")
    assert not s.should_purge()
    dropped = s.purge()
    assert dropped == 0


# ---------------------------------------------------------------------------
# Token bookkeeping round-trip
# ---------------------------------------------------------------------------

def test_projected_tokens_anchors_to_observed_value():
    s = _stream()
    s.append_chunk_text(0, "user", "hi")
    s.update_observed_tokens(1000)
    # No events after observation → projection equals observation.
    assert s.projected_tokens() == 1000
    # Append more events → projection must grow.
    s.append_chunk_text(1, "assistant", "x" * 200)
    assert s.projected_tokens() > 1000


def test_projected_tokens_without_observation_estimates_whole_prompt():
    s = _stream()
    s.append_chunk_text(0, "user", "hello world")
    # Without observation, we fall back to whole-prompt estimate.
    # Just assert it's positive; exact value depends on chars_per_token.
    assert s.projected_tokens() > 0


def test_update_observed_tokens_resets_on_each_call():
    s = _stream()
    s.append_chunk_text(0, "user", "a")
    s.update_observed_tokens(100)
    s.append_chunk_text(1, "user", "b")
    s.update_observed_tokens(120)
    # Observation is now anchored at 120 for len(timeline)==2.
    assert s._observed_prefix_tokens == 120
    assert s._observed_timeline_len == 2
    # No events after → projection equals observation.
    assert s.projected_tokens() == 120


# ---------------------------------------------------------------------------
# Per-event token attribution (powers accurate purge walks)
# ---------------------------------------------------------------------------

def test_event_tokens_start_at_zero_and_stay_aligned_with_timeline():
    s = _stream()
    assert s._event_tokens == []
    s.append_chunk_text(0, "user", "hi")
    s.append_artifact_write(1, "a.md", "body")
    s.append_artifact_edit(2, "a.md", old_string="body", new_string="body2")
    s.append_hunch(3, "h-1", Hunch(smell="s", description="d"))
    s.append_label(4, hunch_id="h-1", label="good")
    assert len(s._event_tokens) == len(s.timeline) == 5
    assert all(t == 0.0 for t in s._event_tokens)  # no observation yet


def test_update_observed_tokens_distributes_timeline_portion_by_char_weight():
    """Longer-rendered events receive a proportionally larger share of
    the observed timeline tokens."""
    s = _stream()
    s.append_chunk_text(0, "user", "a" * 50)      # short
    s.append_chunk_text(1, "user", "b" * 500)     # ~10× longer
    s.update_observed_tokens(1000)
    # Both attributed nonzero, longer event gets more.
    assert s._event_tokens[0] > 0
    assert s._event_tokens[1] > 0
    assert s._event_tokens[1] > 5 * s._event_tokens[0]


def test_empirical_chars_per_token_needs_two_observations():
    """Delta-based ratio requires two consecutive observations to
    cancel the system-prompt overhead. First observation alone doesn't
    set the empirical ratio."""
    s = _stream()
    s.append_chunk_text(0, "user", "x" * 350)
    s.update_observed_tokens(100)
    # First obs: no prior → no delta → empirical still None.
    assert s._empirical_chars_per_token is None
    s.append_chunk_text(1, "user", "y" * 350)
    s.update_observed_tokens(200)
    # Second obs: delta_chars > 0, delta_tokens > 0 → empirical set.
    assert s._empirical_chars_per_token is not None
    assert s._empirical_chars_per_token > 0


def test_empirical_chars_per_token_ema_moves_toward_new_observations():
    s = _stream()
    s.append_chunk_text(0, "user", "a" * 500)
    s.update_observed_tokens(100)
    # First obs → no empirical yet.
    s.append_chunk_text(1, "user", "b" * 500)
    s.update_observed_tokens(200)
    # Second obs → empirical set from first delta.
    first = s._empirical_chars_per_token
    assert first is not None
    # Append a tiny event and observe with a much larger token delta.
    s.append_chunk_text(2, "user", "c" * 500)
    s.update_observed_tokens(1000)  # 800 token delta for ~500 char delta → low ratio
    second = s._empirical_chars_per_token
    assert second is not None
    assert second < first  # moved down toward the lower ratio


def test_projected_tokens_uses_empirical_ratio_for_new_events():
    """After two observations establish an empirical ratio lower than
    the default 3.5, newly appended events are projected using that
    empirical ratio — producing more tokens per char."""
    s = CriticPromptStream(preamble="SYSTEM", chars_per_token=3.5)
    # Two observations to establish delta-based empirical ratio.
    s.append_chunk_text(0, "user", "x" * 1000)
    s.update_observed_tokens(500)
    s.append_chunk_text(1, "user", "y" * 1000)
    s.update_observed_tokens(900)  # delta ~1000 chars / 400 tokens → ~2.5 cpt
    assert s._empirical_chars_per_token is not None
    assert s._empirical_chars_per_token < 3.5
    baseline = s.projected_tokens()
    # Append more and verify projection uses the smaller empirical ratio:
    # same delta chars should produce MORE projected tokens than the
    # default 3.5 estimate would.
    s.append_chunk_text(2, "user", "z" * 100)
    delta_empirical = s.projected_tokens() - baseline
    import math
    delta_default = max(1, math.floor(len(s.timeline[-1].text) / 3.5) + 1)
    assert delta_empirical >= delta_default


def test_purge_uses_observed_event_tokens_when_available():
    """When per-event observations say the timeline is bigger than the
    char-based estimate, purge should drop MORE events than it would
    under the estimate alone."""
    s = CriticPromptStream(
        preamble="P", low_watermark=400, high_watermark=600, chars_per_token=3.5,
    )
    for i in range(50):
        s.append_chunk_text(i, "user", "x" * 60)
    # Simulate an observation where real tokens came in much higher than
    # the chars/3.5 estimate would predict (markdown-dense prompt).
    s.update_observed_tokens(2000)
    # Sum of _event_tokens should reflect the big observation.
    assert sum(s._event_tokens) > 1000
    dropped = s.purge()
    # Heavily over budget → substantial chunk trimmed.
    assert dropped > 20
    # Post-purge synthesized observation stays near the low watermark
    # plus the system overhead (which is large here because the high
    # observation on a tiny prompt creates a big overhead estimate).
    assert s._observed_prefix_tokens is not None
    overhead = int(s._system_overhead_tokens or 0)
    assert s._observed_prefix_tokens <= s.low_watermark + overhead + 50


def test_purge_preserves_event_tokens_for_kept_events():
    s = _make_small_stream()
    for i in range(80):
        s.append_chunk_text(i, "user", "x" * 40)
    s.update_observed_tokens(700)
    sum_before = sum(s._event_tokens)
    s.purge()
    # Kept events' tokens are preserved (not reset).
    assert len(s._event_tokens) == len(s.timeline)
    # Sum of kept tokens should be <= sum of all pre-purge tokens.
    assert sum(s._event_tokens) <= sum_before
    # ... and meaningful (we kept some observed events).
    assert sum(s._event_tokens) > 0


# ---------------------------------------------------------------------------
# System overhead tracking
# ---------------------------------------------------------------------------

def test_system_overhead_is_set_on_first_observation():
    s = CriticPromptStream(preamble="P" * 100, chars_per_token=3.5)
    s.append_chunk_text(1, "user", "hello " * 20)
    assert s._system_overhead_tokens is None
    # Simulate observation with overhead: the model sees 500 tokens but
    # our render is only ~230 chars → ~66 tokens at cpt=3.5.
    # Overhead ≈ 500 - 230/3.5 ≈ 434
    s.update_observed_tokens(500)
    assert s._system_overhead_tokens is not None
    assert s._system_overhead_tokens > 0


def test_system_overhead_ema_smooths_across_observations():
    s = CriticPromptStream(preamble="P" * 100, chars_per_token=3.5)
    s.append_chunk_text(1, "user", "hello " * 20)
    s.update_observed_tokens(500)
    first = s._system_overhead_tokens

    s.append_chunk_text(2, "user", "world " * 30)
    s.update_observed_tokens(700)
    second = s._system_overhead_tokens

    # EMA should have moved from the first value.
    assert second != first


def test_overhead_improves_purge_synthesis():
    """Post-purge projected_tokens is closer to the real observation
    when overhead is tracked vs when it isn't."""
    s = CriticPromptStream(
        preamble="P" * 500,
        low_watermark=2000,
        high_watermark=3000,
        chars_per_token=3.5,
    )
    for i in range(100):
        s.append_chunk_text(i, "user", "x" * 40)
    # Simulate a model observation with significant overhead (~500 tok).
    s.update_observed_tokens(5000)
    assert s._system_overhead_tokens is not None
    overhead = s._system_overhead_tokens

    s.purge()
    synth = s._observed_prefix_tokens

    # The synthesis should include the overhead — it should be at least
    # low_watermark-level (the overhead alone pushes it above the
    # char-only estimate).
    assert synth > s.low_watermark - 100  # not drastically below target
    # And not wildly above.
    assert synth < s.low_watermark + overhead + 200


# ---------------------------------------------------------------------------
# Render ordering — regions appear in canonical order
# ---------------------------------------------------------------------------

def test_render_includes_suffix_when_set():
    s = CriticPromptStream(preamble="PRE", suffix="END MARKER")
    out = s.render()
    assert out.endswith("END MARKER")


def test_load_prompt_template_splits_on_marker(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("HEADER\n<!-- INPUTS_GO_HERE -->\nFOOTER")
    head, tail = load_prompt_template(p)
    assert head.strip() == "HEADER"
    assert tail.strip() == "FOOTER"


def test_load_prompt_template_handles_missing_marker(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("JUST HEADER\n")
    head, tail = load_prompt_template(p)
    assert "JUST HEADER" in head
    assert tail == ""


def test_nose_v1_prompt_loads_with_marker():
    """Regression: the packaged nose_v1 template must have the marker."""
    from pathlib import Path
    import hunch.critic as pkg
    path = Path(pkg.__file__).parent / "prompts" / "nose_v1.md"
    head, tail = load_prompt_template(path)
    assert "You are the Critic" in head
    assert "Respond with ONLY the JSON array" in tail


def test_render_regions_in_canonical_order():
    s = CriticPromptStream(preamble="PRE")
    s.surviving_hunches = [
        InlineHunchEvent(
            tick_seq=0, hunch_id="h-1", smell="SURV", description="d"
        )
    ]
    s.living_artifacts = {"a.md": "ARTCONTENT"}
    s.append_chunk_text(5, "user", "TIMELINE")
    out = s.render()
    i_preamble = out.index("PRE")
    i_surv = out.index("SURV")
    i_art = out.index("ARTCONTENT")
    i_timeline = out.index("TIMELINE")
    assert i_preamble < i_surv < i_art < i_timeline
