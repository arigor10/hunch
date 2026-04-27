"""Tests for hunch.filter — novelty + dedup post-critic filter.

Tests use a fake LLM client to avoid real API calls. The filter's
correctness depends on the LLM's judgment; these tests verify the
wiring: prompt assembly, response parsing, pass/fail routing, dedup
window management, and integration with the journal writer.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.filter.core import (
    FilterResult,
    HunchFilter,
    _parse_json_response,
    _render_dialogue,
)
from hunch.journal.append import append_json_line
from hunch.journal.hunches import HunchRecord


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------

@dataclass
class _FakeMessage:
    text: str


@dataclass
class _FakeResponse:
    content: list[_FakeMessage]


class _FakeClient:
    """Returns canned responses. Responses are popped in order (thread-safe)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.call_log: list[dict[str, Any]] = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs: Any) -> _FakeResponse:
        with self._lock:
            self.call_log.append(kwargs)
            text = self._responses.pop(0) if self._responses else "{}"
        return _FakeResponse(content=[_FakeMessage(text=text)])


def _hunch(smell: str, desc: str = "description") -> Hunch:
    return Hunch(
        smell=smell,
        description=desc,
        triggering_refs=TriggeringRefs(),
    )


# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    assert _parse_json_response('{"duplicate": true}') == {"duplicate": True}


def test_parse_fenced_json():
    text = '```json\n{"duplicate": false}\n```'
    assert _parse_json_response(text) == {"duplicate": False}


def test_parse_json_with_preamble():
    text = 'Here is my answer:\n{"already_raised": true, "who": "researcher"}'
    result = _parse_json_response(text)
    assert result is not None
    assert result["already_raised"] is True


def test_parse_garbage_returns_none():
    assert _parse_json_response("not json at all") is None


# ---------------------------------------------------------------------------
# _render_dialogue
# ---------------------------------------------------------------------------

def test_render_dialogue_filters_by_bookmark(tmp_path: Path):
    conv = tmp_path / "conversation.jsonl"
    for seq, text in [(1, "hello"), (2, "world"), (3, "too far")]:
        append_json_line(conv, {
            "tick_seq": seq,
            "type": "assistant_text" if seq % 2 else "user_text",
            "text": text,
        })
    rendered = _render_dialogue(conv, bookmark_prev=0, bookmark_now=2)
    assert "hello" in rendered
    assert "world" in rendered
    assert "too far" not in rendered


def test_render_dialogue_skips_non_dialogue(tmp_path: Path):
    conv = tmp_path / "conversation.jsonl"
    append_json_line(conv, {
        "tick_seq": 1, "type": "artifact_write", "text": "should skip",
    })
    append_json_line(conv, {
        "tick_seq": 2, "type": "user_text", "text": "visible",
    })
    rendered = _render_dialogue(conv, bookmark_prev=0, bookmark_now=5)
    assert "should skip" not in rendered
    assert "visible" in rendered


def test_render_dialogue_includes_divider(tmp_path: Path):
    conv = tmp_path / "conversation.jsonl"
    for seq, text in [(1, "before"), (2, "also before"), (3, "in window")]:
        append_json_line(conv, {
            "tick_seq": seq,
            "type": "assistant_text",
            "text": text,
        })
    rendered = _render_dialogue(conv, bookmark_prev=2, bookmark_now=3)
    assert "before" in rendered
    assert "--- begin triggering window" in rendered
    assert "in window" in rendered
    lines = rendered.split("\n\n")
    divider_idx = next(i for i, l in enumerate(lines) if "triggering window" in l)
    window_idx = next(i for i, l in enumerate(lines) if "in window" in l)
    assert divider_idx < window_idx


def test_render_dialogue_missing_file(tmp_path: Path):
    assert _render_dialogue(tmp_path / "nope.jsonl", bookmark_prev=0, bookmark_now=10) == ""


# ---------------------------------------------------------------------------
# HunchFilter — dedup
# ---------------------------------------------------------------------------

def test_dedup_filters_duplicate(tmp_path: Path):
    client = _FakeClient([
        '{"duplicate": true, "reasoning": "same concern"}',
    ])
    filt = HunchFilter(replay_dir=tmp_path, client=client, enabled=True)
    filt.init_from_existing([
        HunchRecord(
            hunch_id="h-0001", emitted_ts="", emitted_by_tick=1,
            bookmark_prev=0, bookmark_now=5,
            smell="calibration drift",
            description="calibration looks off",
            triggering_refs={}, status="pending",
        ),
    ])
    results = filt.filter_batch([_hunch("calibration drift again")], bookmark_prev=0, bookmark_now=10)
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].filter_type == "dedup"


def test_dedup_passes_novel_hunch(tmp_path: Path):
    client = _FakeClient([
        '{"duplicate": false, "reasoning": "different concerns"}',
        '{"already_raised": false, "who": null, "reasoning": "not raised"}',
    ])
    filt = HunchFilter(replay_dir=tmp_path, client=client, enabled=True)
    filt.init_from_existing([
        HunchRecord(
            hunch_id="h-0001", emitted_ts="", emitted_by_tick=1,
            bookmark_prev=0, bookmark_now=5,
            smell="calibration drift",
            description="calibration looks off",
            triggering_refs={}, status="pending",
        ),
    ])
    results = filt.filter_batch([_hunch("data leakage")], bookmark_prev=0, bookmark_now=10)
    assert len(results) == 1
    assert results[0].passed is True


def test_dedup_window_limits_comparisons(tmp_path: Path):
    responses = [
        '{"duplicate": false, "reasoning": "different"}',
        '{"already_raised": false, "who": null, "reasoning": "not raised"}',
    ]
    client = _FakeClient(responses)
    filt = HunchFilter(
        replay_dir=tmp_path, client=client, enabled=True, dedup_window=2,
    )
    priors = [
        HunchRecord(
            hunch_id=f"h-{i:04d}", emitted_ts="", emitted_by_tick=i,
            bookmark_prev=0, bookmark_now=i,
            smell=f"concern {i}", description=f"desc {i}",
            triggering_refs={}, status="pending",
        )
        for i in range(1, 6)
    ]
    filt.init_from_existing(priors)
    filt.filter_batch([_hunch("new thing")], bookmark_prev=0, bookmark_now=20)
    dedup_calls = [c for c in client.call_log if "Hunch A" in c.get("messages", [{}])[0].get("content", "")]
    assert len(dedup_calls) <= 2


# ---------------------------------------------------------------------------
# HunchFilter — novelty
# ---------------------------------------------------------------------------

def test_novelty_filters_already_raised(tmp_path: Path):
    conv = tmp_path / "conversation.jsonl"
    append_json_line(conv, {
        "tick_seq": 1, "type": "user_text",
        "text": "I noticed the calibration looks wrong",
    })
    client = _FakeClient([
        '{"already_raised": true, "who": "scientist", "reasoning": "scientist flagged it"}',
    ])
    filt = HunchFilter(replay_dir=tmp_path, client=client, enabled=True)
    results = filt.filter_batch([_hunch("calibration drift")], bookmark_prev=0, bookmark_now=5)
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].filter_type == "novelty"


def test_novelty_passes_when_not_raised(tmp_path: Path):
    conv = tmp_path / "conversation.jsonl"
    append_json_line(conv, {
        "tick_seq": 1, "type": "user_text",
        "text": "let's run the next experiment",
    })
    client = _FakeClient([
        '{"already_raised": false, "who": null, "reasoning": "not discussed"}',
    ])
    filt = HunchFilter(replay_dir=tmp_path, client=client, enabled=True)
    results = filt.filter_batch([_hunch("calibration drift")], bookmark_prev=0, bookmark_now=5)
    assert len(results) == 1
    assert results[0].passed is True


# ---------------------------------------------------------------------------
# HunchFilter — disabled
# ---------------------------------------------------------------------------

def test_filter_disabled_passes_everything(tmp_path: Path):
    filt = HunchFilter(replay_dir=tmp_path, enabled=False)
    results = filt.filter_batch(
        [_hunch("a"), _hunch("b")], bookmark_prev=0, bookmark_now=10,
    )
    assert all(r.passed for r in results)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# HunchFilter — batch behavior
# ---------------------------------------------------------------------------

def test_passing_hunch_added_to_dedup_window(tmp_path: Path):
    # Need conversation.jsonl so novelty check actually runs for hunch 1
    conv = tmp_path / "conversation.jsonl"
    append_json_line(conv, {
        "tick_seq": 1, "type": "user_text", "text": "let's go",
    })
    client = _FakeClient([
        # hunch 1: no priors → skip dedup; novelty passes
        '{"already_raised": false, "who": null, "reasoning": "novel"}',
        # hunch 2: dedup against hunch 1 → duplicate
        '{"duplicate": true, "reasoning": "same as first"}',
    ])
    filt = HunchFilter(replay_dir=tmp_path, client=client, enabled=True)
    batch = [_hunch("concern A"), _hunch("concern A again")]
    results = filt.filter_batch(batch, bookmark_prev=0, bookmark_now=10)
    assert results[0].passed is True
    assert results[1].passed is False
    assert results[1].filter_type == "dedup"


def test_filtered_hunch_not_added_to_dedup_window(tmp_path: Path):
    conv = tmp_path / "conversation.jsonl"
    append_json_line(conv, {
        "tick_seq": 1, "type": "user_text",
        "text": "I already noticed the old concern",
    })
    client = _FakeClient([
        # hunch 1: no priors → skip dedup; novelty says already raised
        '{"already_raised": true, "who": "scientist", "reasoning": "raised"}',
        # hunch 2: no priors (hunch 1 was filtered, not added); novelty passes
        '{"already_raised": false, "who": null, "reasoning": "novel"}',
    ])
    filt = HunchFilter(replay_dir=tmp_path, client=client, enabled=True)
    batch = [_hunch("old concern"), _hunch("new concern")]
    results = filt.filter_batch(batch, bookmark_prev=0, bookmark_now=10)
    assert results[0].passed is False
    assert results[1].passed is True
    assert len(filt._prior_hunches) == 1


# ---------------------------------------------------------------------------
# HunchFilter — LLM error resilience
# ---------------------------------------------------------------------------

def test_llm_error_passes_hunch_through(tmp_path: Path):
    class _BrokenClient:
        @property
        def messages(self):
            return self

        def create(self, **kwargs: Any) -> None:
            raise RuntimeError("API down")

    filt = HunchFilter(replay_dir=tmp_path, client=_BrokenClient(), enabled=True)
    results = filt.filter_batch([_hunch("should pass")], bookmark_prev=0, bookmark_now=5)
    assert len(results) == 1
    assert results[0].passed is True


def test_unparseable_response_passes_hunch(tmp_path: Path):
    conv = tmp_path / "conversation.jsonl"
    append_json_line(conv, {
        "tick_seq": 1, "type": "user_text", "text": "hi",
    })
    client = _FakeClient(["not valid json at all"])
    filt = HunchFilter(replay_dir=tmp_path, client=client, enabled=True)
    results = filt.filter_batch([_hunch("concern")], bookmark_prev=0, bookmark_now=5)
    assert results[0].passed is True


# ---------------------------------------------------------------------------
# Integration: replay driver with filter
# ---------------------------------------------------------------------------

def test_replay_from_dir_applies_filter(tmp_path: Path):
    """End-to-end: run_replay_from_dir with a filter that blocks a hunch
    as already-raised. Verify hunches.jsonl contains both emit and
    filtered events."""
    import datetime as _dt
    from hunch.replay import run_replay, run_replay_from_dir
    from hunch.trigger import TriggerV1Config

    class _TwoHunchCritic:
        def init(self, config): pass
        def shutdown(self): pass

        def tick(self, tick_id, bookmark_prev, bookmark_now):
            return [
                _hunch("novel concern", "this is new"),
                _hunch("stale concern", "scientist already said this"),
            ]

    replay_dir = tmp_path / "replay"
    events = [
        {"type": "user_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 0, tzinfo=_dt.timezone.utc).isoformat(), "text": "hi"},
        {"type": "assistant_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 1, tzinfo=_dt.timezone.utc).isoformat(), "text": "hello"},
        {"type": "user_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 10, tzinfo=_dt.timezone.utc).isoformat(), "text": "next"},
    ]
    # Populate replay dir with events (using stub critic)
    from hunch.critic.stub import StubCritic
    run_replay(
        events=events, project_roots=["/tmp"],
        replay_dir=replay_dir, critic=StubCritic(),
    )
    (replay_dir / "hunches.jsonl").unlink(missing_ok=True)

    # Filter: first hunch passes novelty, second is "already raised"
    client = _FakeClient([
        # hunch 1: no priors → skip dedup; novelty passes
        '{"already_raised": false, "who": null, "reasoning": "novel"}',
        # hunch 2: dedup against hunch 1 → not dup
        '{"duplicate": false, "reasoning": "different"}',
        # hunch 2: novelty → already raised
        '{"already_raised": true, "who": "scientist", "reasoning": "said it"}',
    ])
    hunch_filter = HunchFilter(
        replay_dir=replay_dir, client=client, enabled=True,
    )

    cfg = TriggerV1Config(min_debounce_s=30.0)
    result = run_replay_from_dir(
        replay_dir=replay_dir,
        critic=_TwoHunchCritic(),
        trigger_config=cfg,
        overwrite_hunches=True,
        hunch_filter=hunch_filter,
    )

    assert result.hunches_emitted >= 1

    import json
    lines = [
        json.loads(l) for l in
        (replay_dir / "hunches.jsonl").read_text().strip().splitlines()
    ]
    emits = [l for l in lines if l["type"] == "emit"]
    filtered = [l for l in lines if l["type"] == "filtered"]
    assert len(emits) >= 1
    assert len(filtered) >= 1
    assert filtered[0]["filter_type"] == "novelty"


def test_cross_tick_dedup_in_replay(tmp_path: Path):
    """Hunches emitted in tick 1 should be deduplicated against in tick 2."""
    import datetime as _dt
    from hunch.replay import run_replay, run_replay_from_dir
    from hunch.trigger import TriggerV1Config

    tick_count = 0

    class _RepeatingCritic:
        def init(self, config): pass
        def shutdown(self): pass

        def tick(self, tick_id, bookmark_prev, bookmark_now):
            nonlocal tick_count
            tick_count += 1
            return [_hunch("same concern", "always the same")]

    replay_dir = tmp_path / "replay"
    # Two turns → two ticks (turn-end mode)
    events = [
        {"type": "user_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 0, tzinfo=_dt.timezone.utc).isoformat(), "text": "hi"},
        {"type": "assistant_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 1, tzinfo=_dt.timezone.utc).isoformat(), "text": "hello"},
        {"type": "user_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 10, tzinfo=_dt.timezone.utc).isoformat(), "text": "more"},
        {"type": "assistant_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 11, tzinfo=_dt.timezone.utc).isoformat(), "text": "sure"},
        {"type": "user_text", "timestamp": _dt.datetime(2026, 1, 1, 0, 20, tzinfo=_dt.timezone.utc).isoformat(), "text": "again"},
    ]
    from hunch.critic.stub import StubCritic
    run_replay(
        events=events, project_roots=["/tmp"],
        replay_dir=replay_dir, critic=StubCritic(),
    )
    (replay_dir / "hunches.jsonl").unlink(missing_ok=True)

    client = _FakeClient([
        # tick 1 hunch: no priors → skip dedup; novelty passes
        '{"already_raised": false, "who": null, "reasoning": "novel"}',
        # tick 2 hunch: dedup against tick 1's hunch → duplicate
        '{"duplicate": true, "reasoning": "same concern as before"}',
    ])
    hunch_filter = HunchFilter(
        replay_dir=replay_dir, client=client, enabled=True,
    )

    cfg = TriggerV1Config(min_debounce_s=30.0)
    result = run_replay_from_dir(
        replay_dir=replay_dir,
        critic=_RepeatingCritic(),
        trigger_config=cfg,
        overwrite_hunches=True,
        hunch_filter=hunch_filter,
    )

    assert tick_count == 2
    assert result.hunches_emitted == 1

    import json
    lines = [
        json.loads(l) for l in
        (replay_dir / "hunches.jsonl").read_text().strip().splitlines()
    ]
    emits = [l for l in lines if l["type"] == "emit"]
    filtered = [l for l in lines if l["type"] == "filtered"]
    assert len(emits) == 1
    assert len(filtered) == 1
    assert filtered[0]["filter_type"] == "dedup"


# ---------------------------------------------------------------------------
# Online / offline parity
# ---------------------------------------------------------------------------

# Fields that must match between online and offline hunches.jsonl records.
# Timestamps and bookmarks may differ (different conversation setup), but
# everything the filter writes must be identical.
_PARITY_FIELDS = [
    "type", "smell", "description", "hunch_id",
    "filter_type", "filter_reason", "duplicate_of",
]


def _normalize_record(d: dict) -> dict:
    """Extract only the fields that should be identical across pipelines."""
    return {k: d.get(k, None) for k in _PARITY_FIELDS}


def _parse_hunch_records(hunches_path: Path) -> list[dict]:
    """Read hunches.jsonl and return non-meta records in order."""
    records = []
    for line in hunches_path.read_text().strip().splitlines():
        d = json.loads(line)
        if d.get("type") in ("emit", "filtered"):
            records.append(d)
    return records


def test_online_offline_filter_parity(tmp_path: Path):
    """Online (Runner) and offline (run_replay_from_dir) paths produce
    identical hunches.jsonl records for the same critic output and
    filter responses.

    This catches drift between the two persist loops — e.g. one path
    forgetting to pass duplicate_of or using different field names.
    """
    import datetime as _dt
    from hunch.replay import run_replay, run_replay_from_dir
    from hunch.trigger import TriggerV1Config
    from hunch.run import RunConfig, Runner
    from hunch.journal.append import append_json_line

    class _TwoHunchCritic:
        """Always returns one novel + one stale hunch."""
        def init(self, config): pass
        def shutdown(self): pass
        def tick(self, tick_id, bookmark_prev, bookmark_now):
            return [
                _hunch("novel concern", "this is genuinely new"),
                _hunch("stale concern", "scientist already discussed this"),
            ]

    # Filter responses: hunch 1 passes both checks, hunch 2 fails novelty.
    def _make_filter_responses() -> list[str]:
        return [
            # hunch 1: no priors → skip dedup; novelty → passes
            '{"already_raised": false, "who": null, "reasoning": "novel"}',
            # hunch 2: dedup vs hunch 1 → not a dup
            '{"duplicate": false, "reasoning": "different concerns"}',
            # hunch 2: novelty → already raised
            '{"already_raised": true, "who": "scientist", '
            '"reasoning": "discussed in tick 2"}',
        ]

    events = [
        {"type": "user_text",
         "timestamp": _dt.datetime(2026, 1, 1, 0, 0,
                                   tzinfo=_dt.timezone.utc).isoformat(),
         "text": "hi"},
        {"type": "assistant_text",
         "timestamp": _dt.datetime(2026, 1, 1, 0, 1,
                                   tzinfo=_dt.timezone.utc).isoformat(),
         "text": "hello"},
        {"type": "user_text",
         "timestamp": _dt.datetime(2026, 1, 1, 0, 10,
                                   tzinfo=_dt.timezone.utc).isoformat(),
         "text": "next"},
    ]
    trigger_cfg = TriggerV1Config(min_debounce_s=30.0)

    # ---- OFFLINE path ----
    offline_replay = tmp_path / "offline_replay"
    from hunch.critic.stub import StubCritic
    run_replay(
        events=events, project_roots=["/tmp"],
        replay_dir=offline_replay, critic=StubCritic(),
    )
    (offline_replay / "hunches.jsonl").unlink(missing_ok=True)

    offline_filter = HunchFilter(
        replay_dir=offline_replay,
        client=_FakeClient(_make_filter_responses()),
        enabled=True,
    )
    run_replay_from_dir(
        replay_dir=offline_replay,
        critic=_TwoHunchCritic(),
        trigger_config=trigger_cfg,
        overwrite_hunches=True,
        hunch_filter=offline_filter,
    )
    offline_records = _parse_hunch_records(offline_replay / "hunches.jsonl")

    # ---- ONLINE path ----
    online_dir = tmp_path / "online"
    online_dir.mkdir()
    transcript = online_dir / "transcript.jsonl"
    # Minimal Claude Code transcript lines that parse to text events.
    with open(transcript, "w") as f:
        f.write(json.dumps({
            "uuid": "u1",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "hello"}]},
            "cwd": "/tmp",
        }) + "\n")

    config = RunConfig(
        cwd=online_dir,
        transcript_path=transcript,
        replay_dir=online_dir / "replay",
        poll_s=0.0,
        critic_factory=_TwoHunchCritic,
        anthropic_client=_FakeClient(_make_filter_responses()),
        trigger_config=TriggerV1Config(min_debounce_s=0.0),
        filter_enabled=True,
    )
    runner = Runner(config=config)
    runner.step_once()  # process assistant text

    # Inject claude_stopped to fire the tick
    tick_seq = runner.writer.tick_seq + 1
    append_json_line(runner.writer.conversation_path, {
        "tick_seq": tick_seq,
        "type": "claude_stopped",
        "timestamp": "2026-01-01T00:05:00Z",
    })
    runner.step_once()  # fires tick → critic → filter → journal
    online_records = _parse_hunch_records(
        online_dir / "replay" / "hunches.jsonl"
    )

    # ---- COMPARE ----
    assert len(offline_records) == len(online_records), (
        f"offline produced {len(offline_records)} records, "
        f"online produced {len(online_records)}"
    )
    for i, (off, on) in enumerate(zip(offline_records, online_records)):
        off_norm = _normalize_record(off)
        on_norm = _normalize_record(on)
        assert off_norm == on_norm, (
            f"Record {i} differs:\n"
            f"  offline: {off_norm}\n"
            f"  online:  {on_norm}"
        )
