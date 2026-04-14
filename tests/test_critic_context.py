"""Tests for `hunch.critic.context` — the per-tick context builder.

Context-building is pure: it reads the replay buffer and formats it
into strings the prompt template splices in. No API calls here, so
these tests run offline and stay fast.
"""

from __future__ import annotations

from hunch.capture.writer import ReplayBufferWriter
from hunch.critic.context import (
    ContextConfig,
    build_tick_context,
    read_current_artifacts,
    read_recent_conversation,
    render_prior_hunches_block,
)
from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.journal.feedback import FeedbackWriter
from hunch.journal.hunches import HunchesWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _writer(tmp_path):
    return ReplayBufferWriter(replay_dir=tmp_path)


def _text_event(ts: str, role: str, content: str) -> dict:
    return {
        "type": "text",
        "timestamp": ts,
        "role": role,
        "content": content,
    }


def _write_event(ts: str, path: str, content: str) -> dict:
    return {
        "type": "artifact_write",
        "timestamp": ts,
        "path": path,
        "content": content,
    }


# ---------------------------------------------------------------------------
# read_recent_conversation
# ---------------------------------------------------------------------------

def test_read_recent_conversation_empty_file(tmp_path):
    assert read_recent_conversation(tmp_path / "conv.jsonl", last_n=20) == []


def test_read_recent_conversation_returns_last_n(tmp_path):
    w = _writer(tmp_path)
    for i in range(5):
        w.append_events(
            [_text_event(f"2026-04-14T12:00:0{i}Z", "assistant", f"msg {i}")],
            project_roots=[str(tmp_path)],
        )
    recent = read_recent_conversation(w.conversation_path, last_n=3)
    assert len(recent) == 3
    assert "msg 2" in recent[0]
    assert "msg 4" in recent[-1]


def test_read_recent_conversation_renders_artifact_write(tmp_path):
    w = _writer(tmp_path)
    w.append_events(
        [_write_event("2026-04-14T12:00:00Z", str(tmp_path / "notes.md"), "hello")],
        project_roots=[str(tmp_path)],
    )
    recent = read_recent_conversation(w.conversation_path, last_n=10)
    assert len(recent) == 1
    assert "artifact-write" in recent[0]
    assert "notes.md" in recent[0]


def test_read_recent_conversation_tolerates_malformed_lines(tmp_path):
    conv = tmp_path / "conv.jsonl"
    conv.write_text('not json\n{"type":"text","tick_seq":1,"role":"user","content":"hi"}\n')
    recent = read_recent_conversation(conv, last_n=10)
    assert len(recent) == 1
    assert "hi" in recent[0]


# ---------------------------------------------------------------------------
# read_current_artifacts
# ---------------------------------------------------------------------------

def test_read_current_artifacts_empty(tmp_path):
    (tmp_path / "artifacts").mkdir()
    assert read_current_artifacts(
        tmp_path / "artifacts",
        tmp_path / "artifacts.jsonl",
        budget_bytes=10_000,
    ) == []


def test_read_current_artifacts_returns_latest_md(tmp_path):
    w = _writer(tmp_path)
    w.append_events(
        [_write_event("2026-04-14T12:00:00Z", str(tmp_path / "notes.md"), "v1")],
        project_roots=[str(tmp_path)],
    )
    w.append_events(
        [_write_event("2026-04-14T12:01:00Z", str(tmp_path / "notes.md"), "v2")],
        project_roots=[str(tmp_path)],
    )
    arts = read_current_artifacts(
        w.artifacts_dir, w.artifacts_log_path, budget_bytes=10_000
    )
    assert len(arts) == 1
    assert arts[0][1] == "v2"


def test_read_current_artifacts_skips_non_md(tmp_path):
    w = _writer(tmp_path)
    w.append_events(
        [_write_event("2026-04-14T12:00:00Z", str(tmp_path / "data.csv"), "a,b")],
        project_roots=[str(tmp_path)],
    )
    arts = read_current_artifacts(
        w.artifacts_dir, w.artifacts_log_path, budget_bytes=10_000
    )
    assert arts == []


def test_read_current_artifacts_drops_oldest_when_over_budget(tmp_path):
    w = _writer(tmp_path)
    # Two different .md files, older one will be dropped first.
    old = "A" * 500
    new = "B" * 500
    w.append_events(
        [_write_event("2026-04-14T12:00:00Z", str(tmp_path / "old.md"), old)],
        project_roots=[str(tmp_path)],
    )
    w.append_events(
        [_write_event("2026-04-14T12:05:00Z", str(tmp_path / "new.md"), new)],
        project_roots=[str(tmp_path)],
    )
    # Budget allows only one file.
    arts = read_current_artifacts(
        w.artifacts_dir, w.artifacts_log_path, budget_bytes=600
    )
    paths = [p for p, _ in arts]
    assert any(p.endswith("new.md") for p in paths)
    assert not any(p.endswith("old.md") for p in paths)


# ---------------------------------------------------------------------------
# render_prior_hunches_block
# ---------------------------------------------------------------------------

def _emit(writer: HunchesWriter, smell: str) -> str:
    hid = writer.allocate_id()
    writer.write_emit(
        hunch=Hunch(
            smell=smell,
            description="desc",
            triggering_refs=TriggeringRefs(),
        ),
        hunch_id=hid,
        ts="2026-04-14T12:00:00Z",
        emitted_by_tick=1,
    )
    return hid


def test_render_prior_hunches_empty(tmp_path):
    assert render_prior_hunches_block(
        tmp_path / "hunches.jsonl",
        tmp_path / "feedback.jsonl",
        last_m=10,
    ) == "(no prior hunches)"


def test_render_prior_hunches_includes_label(tmp_path):
    hw = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(hw, "smell A")
    _emit(hw, "smell B")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "bad", "2026-04-14T12:05:00Z")

    block = render_prior_hunches_block(
        tmp_path / "hunches.jsonl",
        tmp_path / "feedback.jsonl",
        last_m=10,
    )
    assert "h-0001" in block
    assert "h-0002" in block
    assert "[labeled: bad]" in block


def test_render_prior_hunches_respects_last_m(tmp_path):
    hw = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    for i in range(5):
        _emit(hw, f"smell {i}")
    block = render_prior_hunches_block(
        tmp_path / "hunches.jsonl",
        tmp_path / "feedback.jsonl",
        last_m=2,
    )
    # Only the last two should appear.
    assert "smell 3" in block
    assert "smell 4" in block
    assert "smell 0" not in block


# ---------------------------------------------------------------------------
# build_tick_context (end-to-end of the readers)
# ---------------------------------------------------------------------------

def test_build_tick_context_assembles_all_blocks(tmp_path):
    w = _writer(tmp_path)
    w.append_events(
        [_text_event("2026-04-14T12:00:00Z", "user", "what if?")],
        project_roots=[str(tmp_path)],
    )
    w.append_events(
        [_write_event("2026-04-14T12:01:00Z", str(tmp_path / "notes.md"), "the plan")],
        project_roots=[str(tmp_path)],
    )
    hw = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(hw, "previous smell")

    ctx = build_tick_context(tmp_path, ContextConfig(last_n_chunks=10, last_m_hunches=5))
    assert "what if?" in ctx.recent_chunks_block
    assert "notes.md" in ctx.artifacts_block
    assert "the plan" in ctx.artifacts_block
    assert "previous smell" in ctx.prior_hunches_block


def test_build_tick_context_empty_replay(tmp_path):
    ctx = build_tick_context(tmp_path)
    assert ctx.recent_chunks_block == "(no conversation events yet)"
    assert ctx.prior_hunches_block == "(no prior hunches)"
    assert "no .md artifacts" in ctx.artifacts_block
