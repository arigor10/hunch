"""Tests for the v0.1 Accumulating Sonnet Critic.

These tests build a small on-disk replay buffer per test, inject a
fake SDK client so no real Anthropic call goes out, and exercise:

  - init / tick / shutdown protocol
  - bookmark filtering (don't feed beyond bookmark_now)
  - dry-run mode (no model call, return [])
  - hunches.jsonl / feedback.jsonl sync on each tick
  - conversation cursor advances correctly across ticks
  - response parse failures return [] without crashing

Byte-cursor math + stream composition is covered here; the deep
accumulator purge math is in test_accumulator.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hunch.critic.sonnet import SonnetCritic, SonnetCriticConfig


# ---------------------------------------------------------------------------
# Fake Anthropic client (same shape as test_critic_sonnet.py's)
# ---------------------------------------------------------------------------

class _FakeContentBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens: int = 1000) -> None:
        self.input_tokens = input_tokens
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _FakeMessage:
    def __init__(self, text: str, input_tokens: int = 1000) -> None:
        self.content = [_FakeContentBlock(text)]
        self.usage = _FakeUsage(input_tokens)


class _FakeMessages:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **kwargs):
        self.parent.last_kwargs = kwargs
        self.parent.calls += 1
        if self.parent.responses:
            return _FakeMessage(self.parent.responses.pop(0))
        return _FakeMessage(self.parent.default_response)


class FakeClient:
    def __init__(self, default: str = "[]", responses: list[str] | None = None):
        self.default_response = default
        self.responses = list(responses or [])
        self.calls = 0
        self.last_kwargs: Any = None
        self.messages = _FakeMessages(self)


# ---------------------------------------------------------------------------
# Replay buffer fixture builder
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _make_replay(tmp_path: Path) -> Path:
    """Build a small replay buffer with a few events and one artifact."""
    replay = tmp_path / "replay"
    (replay / "artifacts").mkdir(parents=True)

    # Snapshot file for an artifact_write at tick_seq=3
    snap_name = "docs_plan.md__20260101T000003__aaaaaaaa"
    (replay / "artifacts" / snap_name).write_text(
        "# Plan\n\nStep 1: do a thing.\n"
    )

    conv = [
        {"tick_seq": 1, "type": "user_text",
         "timestamp": "2026-01-01T00:00:01Z",
         "text": "Let's design experiment 1."},
        {"tick_seq": 2, "type": "assistant_text",
         "timestamp": "2026-01-01T00:00:02Z",
         "text": "Got it — I'll draft a plan."},
        {"tick_seq": 3, "type": "artifact_write",
         "timestamp": "2026-01-01T00:00:03Z",
         "path": "docs/plan.md", "snapshot": snap_name,
         "content_hash": "a" * 64},
        {"tick_seq": 4, "type": "assistant_text",
         "timestamp": "2026-01-01T00:00:04Z",
         "text": "Draft saved."},
    ]
    _write_jsonl(replay / "conversation.jsonl", conv)

    art = [
        {"tick_seq": 3, "ts": "2026-01-01T00:00:03Z", "event": "write",
         "path": "docs/plan.md", "snapshot": snap_name,
         "content_hash": "a" * 64},
    ]
    _write_jsonl(replay / "artifacts.jsonl", art)

    return replay


# ---------------------------------------------------------------------------
# init / shutdown / protocol guards
# ---------------------------------------------------------------------------

def test_init_requires_replay_dir():
    c = SonnetCritic(client=FakeClient())
    with pytest.raises(RuntimeError, match="replay_dir"):
        c.init({})


def test_init_twice_rejects(tmp_path):
    replay = _make_replay(tmp_path)
    c = SonnetCritic(client=FakeClient())
    c.init({"replay_dir": str(replay)})
    with pytest.raises(RuntimeError, match="called twice"):
        c.init({"replay_dir": str(replay)})


def test_tick_before_init_raises(tmp_path):
    c = SonnetCritic(client=FakeClient())
    with pytest.raises(RuntimeError, match="before init"):
        c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=1)


# ---------------------------------------------------------------------------
# Bookmark filtering
# ---------------------------------------------------------------------------

def test_tick_only_feeds_events_up_to_bookmark_now(tmp_path):
    replay = _make_replay(tmp_path)
    fake = FakeClient(default="[]")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    # First tick caps at bookmark_now=2 — should see exactly the first
    # two user/assistant events, NOT the artifact-write at tick 3.
    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=2)
    stream = c._stream  # noqa: SLF001 — test introspection
    assert len(stream.timeline) == 2
    assert all(e.tick_seq <= 2 for e in stream.timeline)


def test_second_tick_resumes_cursor(tmp_path):
    replay = _make_replay(tmp_path)
    fake = FakeClient(default="[]")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=2)
    c.tick(tick_id="t-0002", bookmark_prev=2, bookmark_now=4)
    stream = c._stream  # noqa: SLF001
    assert [e.tick_seq for e in stream.timeline] == [1, 2, 3, 4]
    # Artifact-write content was loaded from the snapshot file.
    art_write = [e for e in stream.timeline if e.tick_seq == 3][0]
    assert "# Plan" in art_write.content  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

def test_dry_run_does_not_call_model(tmp_path):
    replay = _make_replay(tmp_path)
    fake = FakeClient()
    logs: list[str] = []
    c = SonnetCritic(
        config=SonnetCriticConfig(dry_run=True),
        client=fake,
        log=logs.append,
    )
    c.init({"replay_dir": str(replay)})

    result = c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)
    assert result == []
    assert fake.calls == 0
    assert any(line.startswith("[dry]") for line in logs)
    # Dry-run line reports prompt size + projected tokens.
    dry_line = [line for line in logs if line.startswith("[dry]")][0]
    assert "prompt_chars=" in dry_line
    assert "proj_tokens=" in dry_line
    assert "window=0..4" in dry_line


# ---------------------------------------------------------------------------
# Model call + parsing
# ---------------------------------------------------------------------------

def test_tick_returns_parsed_hunches(tmp_path):
    replay = _make_replay(tmp_path)
    response = json.dumps([{
        "smell": "draft claims X; earlier data said Y",
        "description": "The plan asserts X, but conversation at c-0002 "
                       "suggested Y. Worth reconciling.",
        "triggering_refs": {"chunks": ["c-0002"],
                             "artifacts": ["docs/plan.md"]},
    }])
    fake = FakeClient(default=response)
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    out = c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)
    assert len(out) == 1
    assert out[0].smell.startswith("draft claims")
    assert out[0].triggering_refs.artifacts == ["docs/plan.md"]
    assert fake.calls == 1


def test_parse_failure_returns_empty(tmp_path):
    replay = _make_replay(tmp_path)
    fake = FakeClient(default="not json at all")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    out = c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)
    assert out == []


def test_model_exception_swallowed(tmp_path):
    class Exploding(FakeClient):
        def __init__(self):
            super().__init__()

            class _M:
                def create(self, **_):
                    raise RuntimeError("boom")
            self.messages = _M()

    replay = _make_replay(tmp_path)
    logs: list[str] = []
    c = SonnetCritic(client=Exploding(), log=logs.append)
    c.init({"replay_dir": str(replay)})

    out = c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)
    assert out == []
    assert any("model call failed" in line for line in logs)


# ---------------------------------------------------------------------------
# Hunches / labels sync
# ---------------------------------------------------------------------------

def test_hunches_jsonl_sync_appends_to_stream(tmp_path):
    replay = _make_replay(tmp_path)
    # Framework wrote an emit event between tick 1 and tick 2.
    _append_jsonl(replay / "hunches.jsonl", [{
        "type": "emit", "hunch_id": "h-0001",
        "ts": "2026-01-01T00:00:05Z",
        "emitted_by_tick": 1,
        "bookmark_prev": 0, "bookmark_now": 2,
        "smell": "a smell",
        "description": "a description",
        "triggering_refs": {"chunks": ["c-0002"], "artifacts": []},
    }])

    fake = FakeClient(default="[]")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)
    stream = c._stream  # noqa: SLF001
    hunch_events = [e for e in stream.timeline
                    if getattr(e, "hunch_id", None) == "h-0001"]
    assert len(hunch_events) == 1
    assert hunch_events[0].smell == "a smell"  # type: ignore[attr-defined]

    # Second tick shouldn't double-sync.
    c.tick(tick_id="t-0002", bookmark_prev=4, bookmark_now=4)
    hunch_events = [e for e in c._stream.timeline  # noqa: SLF001
                    if getattr(e, "hunch_id", None) == "h-0001"]
    assert len(hunch_events) == 1


def test_feedback_jsonl_sync_appends_label(tmp_path):
    replay = _make_replay(tmp_path)
    _append_jsonl(replay / "feedback.jsonl", [{
        "type": "label", "hunch_id": "h-0001",
        "label": "bad", "ts": "2026-01-01T00:00:06Z",
    }])

    fake = FakeClient(default="[]")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)
    stream = c._stream  # noqa: SLF001
    labels = [e for e in stream.timeline
              if type(e).__name__ == "LabelEvent"]
    assert len(labels) == 1
    assert labels[0].label == "bad"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Cursor robustness: partial lines don't advance past them
# ---------------------------------------------------------------------------

def test_partial_line_is_not_consumed(tmp_path):
    replay = tmp_path / "replay"
    (replay / "artifacts").mkdir(parents=True)
    # Valid line, then a partial line with no trailing newline.
    conv_path = replay / "conversation.jsonl"
    conv_path.write_text(
        json.dumps({
            "tick_seq": 1, "type": "user_text",
            "timestamp": "2026-01-01T00:00:01Z", "text": "hi"
        }) + "\n"
        + json.dumps({
            "tick_seq": 2, "type": "assistant_text",
            "timestamp": "2026-01-01T00:00:02Z", "text": "world"
        })  # no trailing newline — simulate in-flight write
    )

    fake = FakeClient(default="[]")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=2)
    # Only seq=1 is fed — seq=2 is mid-write (no newline yet).
    assert [e.tick_seq for e in c._stream.timeline] == [1]  # noqa: SLF001

    # Now the partial line lands fully.
    with conv_path.open("a") as f:
        f.write("\n")
    c.tick(tick_id="t-0002", bookmark_prev=2, bookmark_now=2)
    # Same bookmark, but seq=2 is now complete AND within the window
    # (seq ≤ bookmark_now). The fix: _last_seq_fed tracks events actually
    # fed, not bookmark_now — so seq=2 isn't skipped on retry.
    assert [e.tick_seq for e in c._stream.timeline] == [1, 2]  # noqa: SLF001


def test_event_past_bookmark_is_retried_not_skipped(tmp_path):
    """When conversation.jsonl contains an event with seq > bookmark_now
    (e.g., bookmark not yet advanced in the framework), the cursor
    rewinds so the next tick re-reads it. _last_seq_fed must NOT be
    advanced past the skipped event, or the retry will drop it."""
    replay = tmp_path / "replay"
    (replay / "artifacts").mkdir(parents=True)
    conv_path = replay / "conversation.jsonl"
    conv_path.write_text(
        json.dumps({
            "tick_seq": 1, "type": "user_text",
            "timestamp": "2026-01-01T00:00:01Z", "text": "hi"
        }) + "\n"
        + json.dumps({
            "tick_seq": 2, "type": "assistant_text",
            "timestamp": "2026-01-01T00:00:02Z", "text": "ahead"
        }) + "\n"
    )

    fake = FakeClient(default="[]")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    # Tick with bookmark_now=1 — seq=2 is on disk but outside window.
    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=1)
    assert [e.tick_seq for e in c._stream.timeline] == [1]  # noqa: SLF001

    # Next tick advances bookmark; seq=2 must be picked up.
    c.tick(tick_id="t-0002", bookmark_prev=1, bookmark_now=2)
    assert [e.tick_seq for e in c._stream.timeline] == [1, 2]  # noqa: SLF001


def test_update_observed_tokens_called_after_model(tmp_path):
    """After a successful model call, the critic must feed real token
    counts back to the stream so that projected_tokens stays calibrated.
    Previously this call was missing, causing post-purge projection
    drift and 'Prompt is too long' errors."""
    replay = _make_replay(tmp_path)
    fake = FakeClient(default="[]")
    c = SonnetCritic(client=fake)
    c.init({"replay_dir": str(replay)})

    # Before any tick, no observation anchor exists.
    stream = c._stream  # noqa: SLF001
    assert stream._observed_prefix_tokens is None  # noqa: SLF001

    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)

    # After the tick, the stream should have an observation anchor
    # from the fake client's usage (1000 input_tokens).
    assert stream._observed_prefix_tokens == 1000  # noqa: SLF001
    # And the projected tokens should be anchored near that.
    proj = stream.projected_tokens()
    assert 900 < proj < 1200


def test_update_observed_tokens_not_called_in_dry_run(tmp_path):
    """Dry-run never calls the model, so it must NOT set an observation
    anchor — that would confuse the projection with a made-up value."""
    replay = _make_replay(tmp_path)
    c = SonnetCritic(config=SonnetCriticConfig(dry_run=True))
    c.init({"replay_dir": str(replay)})

    c.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=4)

    stream = c._stream  # noqa: SLF001
    assert stream._observed_prefix_tokens is None  # noqa: SLF001
