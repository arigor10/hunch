"""Tests for hunch.capture.writer.

Covers the replay buffer invariants:
  - artifact_write → snapshot on disk + entries in both JSONL files
  - artifact_edit → applies to in-memory current content, snapshots the result
  - edit-before-known-base → logged, not snapshotted, no crash
  - failed edit (old_string not present) → logged with reason, content unchanged
  - path normalization strips project root; falls back to basename
  - tick_seq is monotonically increasing
  - poll_once ties the parser + writer together
"""

from __future__ import annotations

import json
from pathlib import Path

from hunch.capture import ReplayBufferWriter, poll_once
from hunch.capture.writer import _normalize_artifact_path, _snapshot_filename
from hunch.parse import ParserState


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def test_normalize_artifact_path_strips_project_root():
    assert _normalize_artifact_path(
        "/home/arigor/YoC/proj/docs/notes.md",
        ["/home/arigor/YoC/proj/"],
    ) == "docs/notes.md"


def test_normalize_artifact_path_prefers_longest_match():
    roots = ["/home/arigor/YoC/", "/home/arigor/YoC/proj/"]
    assert _normalize_artifact_path(
        "/home/arigor/YoC/proj/notes.md", roots
    ) == "notes.md"


def test_normalize_artifact_path_falls_back_to_basename():
    assert _normalize_artifact_path(
        "/some/other/dir/file.md", ["/home/arigor/YoC/proj/"],
    ) == "file.md"


# ---------------------------------------------------------------------------
# Snapshot filename
# ---------------------------------------------------------------------------

def test_snapshot_filename_flattens_slashes_and_unsafe_chars():
    name = _snapshot_filename(
        "docs/sub dir/my notes.md",
        "2026-04-13T10:00:00.123456Z",
        "abcd1234" * 8,
    )
    assert "/" not in name
    assert name.startswith("docs_sub_dir_my_notes.md__")
    assert "20260413T100000" in name
    assert name.endswith("__abcd1234")


# ---------------------------------------------------------------------------
# ReplayBufferWriter — basic flow
# ---------------------------------------------------------------------------

def test_writer_creates_directory_layout(tmp_path):
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    assert (tmp_path / "replay" / "artifacts").is_dir()
    assert writer.conversation_path == tmp_path / "replay" / "conversation.jsonl"


def test_writer_handles_plain_text_events(tmp_path):
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    writer.append_events(
        [
            {"type": "user_text", "timestamp": "t1", "text": "hi"},
            {"type": "assistant_text", "timestamp": "t2", "text": "hello"},
        ],
        project_roots=["/home/arigor/YoC/proj/"],
    )
    conv = _read_jsonl(writer.conversation_path)
    assert [e["type"] for e in conv] == ["user_text", "assistant_text"]
    assert [e["tick_seq"] for e in conv] == [1, 2]
    # No artifact events logged.
    assert _read_jsonl(writer.artifacts_log_path) == []


def test_writer_artifact_write_snapshots_and_logs(tmp_path):
    root = "/home/arigor/YoC/proj/"
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    writer.append_events(
        [
            {
                "type": "artifact_write",
                "timestamp": "2026-04-13T10:00:00Z",
                "path": root + "notes.md",
                "content": "# Notes\nfirst",
            },
        ],
        project_roots=[root],
    )
    # Snapshot file exists with correct contents.
    snapshots = list(writer.artifacts_dir.iterdir())
    assert len(snapshots) == 1
    assert snapshots[0].read_text() == "# Notes\nfirst"

    # Both logs carry the event with a relative path and hash.
    conv = _read_jsonl(writer.conversation_path)
    art = _read_jsonl(writer.artifacts_log_path)
    assert len(conv) == len(art) == 1
    assert conv[0]["path"] == "notes.md"
    assert conv[0]["snapshot"] == snapshots[0].name
    assert conv[0]["tick_seq"] == 1
    assert art[0]["event"] == "write"
    assert art[0]["content_hash"] == conv[0]["content_hash"]

    # In-memory current content tracks the file.
    assert writer.current_artifact_content["notes.md"] == "# Notes\nfirst"


def test_writer_artifact_edit_applied_to_current_content(tmp_path):
    root = "/home/arigor/YoC/proj/"
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    writer.append_events(
        [
            {
                "type": "artifact_write",
                "timestamp": "t1",
                "path": root + "notes.md",
                "content": "hello world",
            },
            {
                "type": "artifact_edit",
                "timestamp": "t2",
                "path": root + "notes.md",
                "old_string": "world",
                "new_string": "friend",
            },
        ],
        project_roots=[root],
    )
    # Current content should reflect the edit.
    assert writer.current_artifact_content["notes.md"] == "hello friend"

    # Two snapshots exist (one per mutation), both on disk.
    snapshots = sorted(writer.artifacts_dir.iterdir())
    assert len(snapshots) == 2
    contents = {s.read_text() for s in snapshots}
    assert contents == {"hello world", "hello friend"}

    # Both streams log both events with monotonic tick_seq.
    conv = _read_jsonl(writer.conversation_path)
    art = _read_jsonl(writer.artifacts_log_path)
    assert [c["tick_seq"] for c in conv] == [1, 2]
    assert [a["event"] for a in art] == ["write", "edit"]

    # The edit entry in conversation.jsonl carries the diff.
    edit_entry = conv[1]
    assert edit_entry["type"] == "artifact_edit"
    assert edit_entry["diff"] == {"old_string": "world", "new_string": "friend"}


def test_writer_edit_before_known_base_is_skipped_and_logged(tmp_path):
    root = "/home/arigor/YoC/proj/"
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    writer.append_events(
        [
            {
                "type": "artifact_edit",
                "timestamp": "t1",
                "path": root + "unknown.md",
                "old_string": "x",
                "new_string": "y",
            },
        ],
        project_roots=[root],
    )
    # No snapshot written.
    assert list(writer.artifacts_dir.iterdir()) == []
    # Both logs record the skip.
    conv = _read_jsonl(writer.conversation_path)
    art = _read_jsonl(writer.artifacts_log_path)
    assert conv[0]["skipped_reason"] == "edit_before_known_base"
    assert art[0]["event"] == "edit_skipped"
    assert art[0]["reason"] == "edit_before_known_base"


def test_writer_failed_edit_keeps_content_unchanged(tmp_path):
    root = "/home/arigor/YoC/proj/"
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    writer.append_events(
        [
            {
                "type": "artifact_write",
                "timestamp": "t1",
                "path": root + "notes.md",
                "content": "hello world",
            },
            {
                "type": "artifact_edit",
                "timestamp": "t2",
                "path": root + "notes.md",
                "old_string": "NOT PRESENT",
                "new_string": "whatever",
            },
        ],
        project_roots=[root],
    )
    # Content unchanged.
    assert writer.current_artifact_content["notes.md"] == "hello world"
    # Only the original snapshot exists (edit didn't produce a new one).
    snapshots = list(writer.artifacts_dir.iterdir())
    assert len(snapshots) == 1

    conv = _read_jsonl(writer.conversation_path)
    art = _read_jsonl(writer.artifacts_log_path)
    # First event = write, second = failed edit.
    assert conv[1]["skipped_reason"] == "old_string_not_found"
    assert art[1]["event"] == "edit_failed"


def test_writer_tick_seq_is_monotonic_across_batches(tmp_path):
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    writer.append_events(
        [{"type": "user_text", "timestamp": "t1", "text": "a"}],
        project_roots=[],
    )
    writer.append_events(
        [{"type": "user_text", "timestamp": "t2", "text": "b"}],
        project_roots=[],
    )
    conv = _read_jsonl(writer.conversation_path)
    assert [c["tick_seq"] for c in conv] == [1, 2]


def test_writer_appends_do_not_truncate(tmp_path):
    """Simulate a process-restart scenario: a new writer instance pointed at
    the same replay_dir must not clobber the existing conversation.jsonl.
    """
    replay_dir = tmp_path / "replay"
    w1 = ReplayBufferWriter(replay_dir=replay_dir)
    w1.append_events(
        [{"type": "user_text", "timestamp": "t1", "text": "a"}],
        project_roots=[],
    )
    # New writer — fresh tick_seq, but appends should preserve the old entry.
    w2 = ReplayBufferWriter(replay_dir=replay_dir)
    w2.append_events(
        [{"type": "user_text", "timestamp": "t2", "text": "b"}],
        project_roots=[],
    )
    conv = _read_jsonl(w2.conversation_path)
    assert [c["text"] for c in conv] == ["a", "b"]


# ---------------------------------------------------------------------------
# poll_once end-to-end
# ---------------------------------------------------------------------------

def test_poll_once_end_to_end(transcript_factory, tmp_path):
    yoc_path = "/home/arigor/YoC/example_proj/notes.md"
    t_path = transcript_factory([
        transcript_factory.user_text("hello", "t1"),
        transcript_factory.assistant_tool_use(
            "tu_1", "Write",
            {"file_path": yoc_path, "content": "# Notes"},
            "t2",
        ),
    ])
    writer = ReplayBufferWriter(replay_dir=tmp_path / "replay")
    state = ParserState()
    state = poll_once(t_path, writer, state)

    assert state.line_offset == 2
    assert state.project_roots == ["/home/arigor/YoC/example_proj/"]
    conv = _read_jsonl(writer.conversation_path)
    assert [c["type"] for c in conv] == ["user_text", "artifact_write"]
    assert conv[1]["path"] == "notes.md"

    # Second poll with no new lines → state carried through unchanged.
    state2 = poll_once(t_path, writer, state)
    assert state2.line_offset == 2
    conv_after = _read_jsonl(writer.conversation_path)
    assert len(conv_after) == 2  # No duplicate entries.
