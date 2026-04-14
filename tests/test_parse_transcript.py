"""Tests for hunch.parse.transcript.

Covers both offline (parse_whole_file) and online (poll_new_events)
entry points, including the tricky cases:
  - incremental appends produce only new events
  - a tool_error whose originating tool_use appeared in an earlier poll
    is still matched (via the persistent tool_call_map)
  - noise and continuation messages are filtered
  - figure detection fires only on python commands with plot/savefig/...
  - artifact detection requires the path to live under a project root
"""

from __future__ import annotations

from hunch.parse import (
    ParserState,
    parse_whole_file,
    poll_new_events,
    detect_project_roots,
)


# ---------------------------------------------------------------------------
# parse_whole_file
# ---------------------------------------------------------------------------

def test_parse_whole_file_basic_user_and_assistant(transcript_factory, project_root):
    path = transcript_factory([
        transcript_factory.user_text("What do you think of the data?", "2026-04-13T10:00:00Z"),
        transcript_factory.assistant_text("Looks odd — let me investigate.", "2026-04-13T10:00:01Z"),
    ])
    events, roots = parse_whole_file(path, project_roots=[project_root])
    assert roots == [project_root]
    assert [e["type"] for e in events] == ["user_text", "assistant_text"]
    assert events[0]["text"] == "What do you think of the data?"
    assert events[1]["text"] == "Looks odd — let me investigate."


def test_parse_whole_file_filters_noise_and_continuation(transcript_factory, project_root):
    noise = "<local-command-stdout>ignored</local-command-stdout>"
    cont = "This session is being continued from a previous conversation ..."
    path = transcript_factory([
        transcript_factory.user_text(noise, "t1"),
        transcript_factory.user_text(cont, "t2"),
        transcript_factory.user_text("real question", "t3"),
    ])
    events, _ = parse_whole_file(path, project_roots=[project_root])
    assert len(events) == 1
    assert events[0]["type"] == "user_text"
    assert events[0]["text"] == "real question"


def test_parse_whole_file_artifact_write_detected(transcript_factory, project_root):
    md_path = project_root + "notes.md"
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_1", "Write",
            {"file_path": md_path, "content": "# Notes\n\nfirst pass"},
            "t1",
        ),
    ])
    events, _ = parse_whole_file(path, project_roots=[project_root])
    types = [e["type"] for e in events]
    assert "artifact_write" in types
    write = next(e for e in events if e["type"] == "artifact_write")
    assert write["path"] == md_path
    assert write["content"] == "# Notes\n\nfirst pass"


def test_parse_whole_file_artifact_write_outside_root_is_ignored(transcript_factory, project_root):
    # A .md path that does NOT start with project_root should not be tagged
    # as an artifact event.
    outside_path = "/home/arigor/.claude/CLAUDE.md"
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_1", "Write",
            {"file_path": outside_path, "content": "x"},
            "t1",
        ),
    ])
    events, _ = parse_whole_file(path, project_roots=[project_root])
    assert not any(e["type"] == "artifact_write" for e in events)


def test_parse_whole_file_artifact_edit_detected(transcript_factory, project_root):
    md_path = project_root + "notes.md"
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_1", "Edit",
            {"file_path": md_path, "old_string": "first pass", "new_string": "second pass"},
            "t1",
        ),
    ])
    events, _ = parse_whole_file(path, project_roots=[project_root])
    edits = [e for e in events if e["type"] == "artifact_edit"]
    assert len(edits) == 1
    assert edits[0]["old_string"] == "first pass"
    assert edits[0]["new_string"] == "second pass"


def test_parse_whole_file_figure_command_detected(transcript_factory, project_root):
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_1", "Bash",
            {"command": "python3 scripts/plot_pareto.py --savefig out.png"},
            "t1",
        ),
        transcript_factory.assistant_tool_use(
            "tu_2", "Bash",
            {"command": "ls -la"},
            "t2",
        ),
    ])
    events, _ = parse_whole_file(path, project_roots=[project_root])
    figures = [e for e in events if e["type"] == "figure"]
    assert len(figures) == 1
    assert "plot_pareto.py" in figures[0]["command"]


def test_parse_whole_file_tool_error_matched_to_call(transcript_factory, project_root):
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_err", "Bash", {"command": "false"}, "t1"
        ),
        transcript_factory.user_tool_result(
            "tu_err", "command failed with exit code 1", "t2", is_error=True
        ),
    ])
    events, _ = parse_whole_file(path, project_roots=[project_root])
    errors = [e for e in events if e["type"] == "tool_error"]
    assert len(errors) == 1
    assert errors[0]["tool_name"] == "Bash"
    assert "failed" in errors[0]["error"]


def test_parse_whole_file_auto_detects_yoc_project_root(transcript_factory):
    # Use a path that matches the YoC heuristic explicitly.
    yoc_path = "/home/arigor/YoC/example_proj/notes.md"
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_1", "Write", {"file_path": yoc_path, "content": "hi"}, "t1"
        ),
    ])
    events, roots = parse_whole_file(path)
    assert roots == ["/home/arigor/YoC/example_proj/"]
    # And the write event should be recognized now that auto-detection worked.
    assert any(e["type"] == "artifact_write" for e in events)


# ---------------------------------------------------------------------------
# detect_project_roots
# ---------------------------------------------------------------------------

def test_detect_project_roots_ignores_claude_memory_paths():
    records = [
        {
            "type": "assistant",
            "tool_calls": [
                {
                    "id": "a",
                    "name": "Write",
                    "input": {"file_path": "/home/arigor/YoC/proj/a.md"},
                },
                {
                    "id": "b",
                    "name": "Write",
                    "input": {"file_path": "/home/arigor/.claude/CLAUDE.md"},
                },
            ],
        },
    ]
    roots = detect_project_roots(records)
    assert roots == ["/home/arigor/YoC/proj/"]


# ---------------------------------------------------------------------------
# poll_new_events (incremental)
# ---------------------------------------------------------------------------

def test_poll_new_events_empty_file(tmp_path):
    # Nonexistent file → empty events, state unchanged.
    missing = tmp_path / "nope.jsonl"
    state = ParserState()
    events, new_state = poll_new_events(missing, state)
    assert events == []
    assert new_state is state


def test_poll_new_events_incremental_append(transcript_factory, project_root):
    path = transcript_factory([
        transcript_factory.user_text("hello", "t1"),
    ])
    state = ParserState()
    events1, state = poll_new_events(path, state)
    assert len(events1) == 1
    assert state.line_offset == 1

    # No new data → empty events, state unchanged.
    events_none, state = poll_new_events(path, state)
    assert events_none == []
    assert state.line_offset == 1

    # Append a new record.
    transcript_factory.append(path, [
        transcript_factory.assistant_text("hi there", "t2"),
    ])
    events2, state = poll_new_events(path, state)
    assert len(events2) == 1
    assert events2[0]["type"] == "assistant_text"
    assert state.line_offset == 2


def test_poll_new_events_tool_error_across_polls(transcript_factory, project_root):
    # Tool call in poll 1; error in poll 2. The error should still be
    # matched to the call by tool_use_id via the persistent map.
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_cross", "Bash", {"command": "false"}, "t1"
        ),
    ])
    state = ParserState()
    events1, state = poll_new_events(path, state)
    # The tool_use is a Bash that isn't a figure → no event emitted.
    assert not events1
    assert "tu_cross" in state.tool_call_map

    transcript_factory.append(path, [
        transcript_factory.user_tool_result(
            "tu_cross", "boom", "t2", is_error=True
        ),
    ])
    events2, state = poll_new_events(path, state)
    assert len(events2) == 1
    assert events2[0]["type"] == "tool_error"
    assert events2[0]["tool_name"] == "Bash"
    assert events2[0]["error"] == "boom"


def test_poll_new_events_first_call_detects_project_roots(transcript_factory):
    yoc_path = "/home/arigor/YoC/example_proj/notes.md"
    path = transcript_factory([
        transcript_factory.assistant_tool_use(
            "tu_1", "Write", {"file_path": yoc_path, "content": "hi"}, "t1"
        ),
    ])
    state = ParserState()
    _, state = poll_new_events(path, state)
    assert state.project_roots == ["/home/arigor/YoC/example_proj/"]
