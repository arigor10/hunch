"""Tests for CriticEngine with a FakeBackend.

Exercises the engine's model-agnostic logic (stream feeding, hunch/label
sync, dry-run, failure handling) without coupling to any specific backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hunch.backend.protocol import ModelResponse
from hunch.critic.accumulator import InlineHunchEvent, LabelEvent
from hunch.critic.engine import CriticEngine, CriticEngineConfig


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------

class FakeBackend:
    def __init__(self, default: str = "[]", responses: list[str] | None = None,
                 input_tokens: int = 1000, error: Exception | None = None):
        self.default = default
        self.responses = list(responses or [])
        self.input_tokens = input_tokens
        self.error = error
        self.calls = 0
        self.last_prompt: str | None = None

    def call(self, prompt: str, cache_break: int | None = None) -> ModelResponse:
        self.calls += 1
        self.last_prompt = prompt
        if self.error is not None:
            raise self.error
        text = self.responses.pop(0) if self.responses else self.default
        return ModelResponse(text=text, input_tokens=self.input_tokens)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_replay(tmp_path: Path) -> Path:
    replay = tmp_path / "replay"
    (replay / "artifacts").mkdir(parents=True)
    conv = replay / "conversation.jsonl"
    rows = [
        {"tick_seq": 1, "type": "user_text",
         "timestamp": "2026-01-01T00:00:01Z", "text": "hello"},
        {"tick_seq": 2, "type": "assistant_text",
         "timestamp": "2026-01-01T00:00:02Z", "text": "world"},
        {"tick_seq": 3, "type": "artifact_write",
         "timestamp": "2026-01-01T00:00:03Z",
         "path": "notes.md", "snapshot": "snap_001.md"},
        {"tick_seq": 4, "type": "user_text",
         "timestamp": "2026-01-01T00:00:04Z", "text": "done"},
    ]
    _write_jsonl(conv, rows)
    (replay / "artifacts" / "snap_001.md").write_text("# Notes\nsome content\n")
    return replay


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _make_engine(backend=None, **config_kw) -> CriticEngine:
    if backend is None:
        backend = FakeBackend()
    cfg = CriticEngineConfig(**config_kw)
    return CriticEngine(backend=backend, config=cfg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCriticEngine:
    def test_init_requires_replay_dir(self, tmp_path):
        engine = _make_engine()
        with pytest.raises(RuntimeError, match="replay_dir"):
            engine.init({})

    def test_init_twice_rejects(self, tmp_path):
        replay = _make_replay(tmp_path)
        engine = _make_engine()
        engine.init({"replay_dir": str(replay)})
        with pytest.raises(RuntimeError, match="called twice"):
            engine.init({"replay_dir": str(replay)})

    def test_tick_before_init_raises(self):
        engine = _make_engine()
        with pytest.raises(RuntimeError, match="before init"):
            engine.tick("t-0001", 0, 1)

    def test_basic_tick_calls_backend(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend(default="[]")
        engine = _make_engine(backend=backend)
        engine.init({"replay_dir": str(replay)})
        hunches = engine.tick("t-0001", 0, 4)
        assert hunches == []
        assert backend.calls == 1
        assert backend.last_prompt is not None
        assert "hello" in backend.last_prompt

    def test_dry_run_skips_backend(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend()
        engine = _make_engine(backend=backend, dry_run=True)
        engine.init({"replay_dir": str(replay)})
        hunches = engine.tick("t-0001", 0, 4)
        assert hunches == []
        assert backend.calls == 0

    def test_returns_parsed_hunches(self, tmp_path):
        replay = _make_replay(tmp_path)
        response = json.dumps([
            {"smell": "test smell", "description": "test desc",
             "triggering_refs": {"chunks": ["c-0001"], "artifacts": []}},
        ])
        backend = FakeBackend(default=response)
        engine = _make_engine(backend=backend)
        engine.init({"replay_dir": str(replay)})
        hunches = engine.tick("t-0001", 0, 4)
        assert len(hunches) == 1
        assert hunches[0].smell == "test smell"

    def test_bookmark_filtering(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend(default="[]")
        engine = _make_engine(backend=backend)
        engine.init({"replay_dir": str(replay)})
        engine.tick("t-0001", 0, 2)
        # Only seq 1,2 should be in the timeline (not 3,4)
        assert len(engine._stream.timeline) == 2

    def test_cursor_resumes_across_ticks(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend(default="[]")
        engine = _make_engine(backend=backend)
        engine.init({"replay_dir": str(replay)})
        engine.tick("t-0001", 0, 2)
        assert len(engine._stream.timeline) == 2
        engine.tick("t-0002", 2, 4)
        # Now has all 4 events (seq 1,2 user/assistant + seq 3 artifact + seq 4 user)
        assert len(engine._stream.timeline) == 4

    def test_model_failure_swallowed(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend(error=RuntimeError("boom"))
        engine = _make_engine(backend=backend, max_consecutive_failures=3)
        engine.init({"replay_dir": str(replay)})
        hunches = engine.tick("t-0001", 0, 4)
        assert hunches == []
        assert engine._consecutive_failures == 1

    def test_consecutive_failures_abort(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend(error=RuntimeError("boom"))
        engine = _make_engine(backend=backend, max_consecutive_failures=2)
        engine.init({"replay_dir": str(replay)})
        engine.tick("t-0001", 0, 4)  # failure 1 — swallowed
        with pytest.raises(RuntimeError, match="2 consecutive"):
            engine.tick("t-0002", 0, 4)  # failure 2 — aborts

    def test_success_resets_failure_count(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend(
            responses=["error_placeholder", "[]"],
            error=None,
        )
        # First call fails, second succeeds
        call_count = [0]
        orig_error = RuntimeError("boom")

        class FlakeyBackend:
            calls = 0
            last_prompt = None

            def call(self, prompt, cache_break=None):
                self.calls += 1
                self.last_prompt = prompt
                if self.calls == 1:
                    raise orig_error
                return ModelResponse(text="[]", input_tokens=1000)

        backend = FlakeyBackend()
        engine = _make_engine(backend=backend, max_consecutive_failures=3)
        engine.init({"replay_dir": str(replay)})
        engine.tick("t-0001", 0, 4)  # fails, swallowed
        assert engine._consecutive_failures == 1
        engine.tick("t-0002", 0, 4)  # succeeds
        assert engine._consecutive_failures == 0

    def test_hunch_sync(self, tmp_path):
        replay = _make_replay(tmp_path)
        _append_jsonl(replay / "hunches.jsonl", [{
            "type": "emit", "hunch_id": "h-0001",
            "ts": "2026-01-01T00:00:05Z", "emitted_by_tick": 1,
            "bookmark_prev": 0, "bookmark_now": 2,
            "smell": "synced smell", "description": "synced desc",
            "triggering_refs": {"chunks": [], "artifacts": []},
        }])
        backend = FakeBackend(default="[]")
        engine = _make_engine(backend=backend)
        engine.init({"replay_dir": str(replay)})
        engine.tick("t-0001", 0, 4)
        hunch_events = [e for e in engine._stream.timeline if isinstance(e, InlineHunchEvent)]
        assert len(hunch_events) == 1

    def test_label_sync(self, tmp_path):
        replay = _make_replay(tmp_path)
        _write_jsonl(replay / "hunches.jsonl", [{
            "type": "emit", "hunch_id": "h-0001",
            "ts": "2026-01-01T00:00:05Z", "emitted_by_tick": 1,
            "bookmark_prev": 0, "bookmark_now": 2,
            "smell": "s", "description": "d",
            "triggering_refs": {"chunks": [], "artifacts": []},
        }])
        _write_jsonl(replay / "feedback.jsonl", [{
            "hunch_id": "h-0001", "label": "good", "tick_seq": 3,
        }])
        backend = FakeBackend(default="[]")
        engine = _make_engine(backend=backend)
        engine.init({"replay_dir": str(replay)})
        engine.tick("t-0001", 0, 4)
        label_events = [e for e in engine._stream.timeline if isinstance(e, LabelEvent)]
        assert len(label_events) == 1

    def test_token_calibration(self, tmp_path):
        replay = _make_replay(tmp_path)
        backend = FakeBackend(default="[]", input_tokens=5000)
        engine = _make_engine(backend=backend)
        engine.init({"replay_dir": str(replay)})
        engine.tick("t-0001", 0, 4)
        assert engine._stream._observed_prefix_tokens == 5000

    def test_shutdown(self, tmp_path):
        replay = _make_replay(tmp_path)
        engine = _make_engine()
        engine.init({"replay_dir": str(replay)})
        assert engine._initialized
        engine.shutdown()
        assert not engine._initialized
