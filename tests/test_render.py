"""Tests for hunch.render — shared event rendering."""

from pathlib import Path

import pytest

from hunch.render import (
    fmt_ts,
    read_snapshot,
    render_event,
    render_events,
    truncate,
)


class TestRenderEvent:
    def test_user_text_includes_seq(self):
        event = {"tick_seq": 42, "type": "user_text", "text": "hello"}
        result = render_event(event)
        assert result == "**USER** (seq 42): hello\n"

    def test_assistant_text_includes_seq(self):
        event = {"tick_seq": 7, "type": "assistant_text", "text": "on it"}
        result = render_event(event)
        assert result == "**CLAUDE** (seq 7): on it\n"

    def test_empty_text_returns_none(self):
        assert render_event({"tick_seq": 1, "type": "user_text", "text": ""}) is None
        assert render_event({"tick_seq": 1, "type": "assistant_text", "text": "  "}) is None

    def test_artifact_write_with_snapshot(self, tmp_path):
        (tmp_path / "snap1").write_text("content here")
        event = {"tick_seq": 10, "type": "artifact_write", "path": "doc.md", "snapshot": "snap1"}
        result = render_event(event, tmp_path)
        assert "[ARTIFACT WRITE] (seq 10): doc.md" in result
        assert "[Content (12 chars):]" in result
        assert "content here" in result

    def test_artifact_write_without_snapshot(self):
        event = {"tick_seq": 10, "type": "artifact_write", "path": "doc.md"}
        result = render_event(event)
        assert "[Content unavailable]" in result
        assert "(seq 10)" in result

    def test_artifact_edit_includes_seq(self):
        event = {
            "tick_seq": 15, "type": "artifact_edit", "path": "x.md",
            "diff": {"old_string": "a", "new_string": "b"},
        }
        result = render_event(event)
        assert "[ARTIFACT EDIT] (seq 15): x.md" in result

    def test_tool_error_includes_seq(self):
        event = {"tick_seq": 20, "type": "tool_error", "tool_name": "Bash", "error": "fail"}
        result = render_event(event)
        assert "[TOOL ERROR (Bash)] (seq 20): fail" in result

    def test_figure_includes_seq(self):
        event = {"tick_seq": 30, "type": "figure", "command": "plt.show()"}
        result = render_event(event)
        assert "[FIGURE] (seq 30): plt.show()" in result

    def test_unknown_type_returns_none(self):
        assert render_event({"tick_seq": 1, "type": "bogus"}) is None


class TestRenderEvents:
    def test_joins_rendered_events(self):
        events = [
            {"tick_seq": 1, "type": "user_text", "text": "hi"},
            {"tick_seq": 2, "type": "assistant_text", "text": "hello"},
        ]
        result = render_events(events)
        assert "**USER** (seq 1): hi" in result
        assert "**CLAUDE** (seq 2): hello" in result

    def test_empty_list(self):
        assert render_events([]) == ""


class TestHelpers:
    def test_truncate_short(self):
        assert truncate("abc", 10) == "abc"

    def test_truncate_long(self):
        assert truncate("abcdefghij", 5) == "abcde..."

    def test_read_snapshot_missing_dir(self):
        assert read_snapshot(None, "snap") is None

    def test_read_snapshot_missing_name(self, tmp_path):
        assert read_snapshot(tmp_path, "") is None

    def test_read_snapshot_missing_file(self, tmp_path):
        assert read_snapshot(tmp_path, "nonexistent") is None

    def test_read_snapshot_found(self, tmp_path):
        (tmp_path / "snap1").write_text("data")
        assert read_snapshot(tmp_path, "snap1") == "data"

    def test_fmt_ts(self):
        assert fmt_ts("2026-03-15T14:22:00.123Z") == "2026-03-15 14:22:00"
        assert fmt_ts("") == ""
