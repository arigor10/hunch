"""Parse a Claude Code .jsonl transcript into a typed event stream.

Two entry points:

- `parse_whole_file(path, project_roots=None)` — offline: read the whole
  transcript, return the full event list. Used by the mining pipeline and
  for one-shot analyses.

- `poll_new_events(path, state)` — online: read only lines appended since
  the last poll, return new events + an updated ParserState. Used by the
  live framework's Capture component.

Events emitted:
  - user_text        — the Scientist spoke
  - assistant_text   — the Researcher spoke
  - artifact_write   — Write on a project .md file (full content)
  - artifact_edit    — Edit on a project .md file (old/new strings)
  - figure           — Bash command that likely produces a figure
  - tool_error       — tool call returned an error

Run grouping and chunk splitting (done by the offline miner) are NOT done
here. The live framework consumes the flat event stream directly; grouping
is a view computed at read time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Event type (a typed dict; we keep it dict-shaped for easy JSONL round-trip)
# ---------------------------------------------------------------------------

# An event is a dict with at minimum:
#   {"type": str, "timestamp": str, ...type-specific fields}
# We alias it to `dict` for simplicity; callers can use TypedDict if they
# want stricter typing.
Event = dict[str, Any]


# ---------------------------------------------------------------------------
# Low-level extractors (shared with the offline miner)
# ---------------------------------------------------------------------------

def _extract_text(content: Any) -> str:
    """Pull plain text from a message's content field (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _extract_tool_calls(content: Any) -> list[dict[str, Any]]:
    """Pull tool_use blocks from assistant message content."""
    if not isinstance(content, list):
        return []
    calls = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
    return calls


def _extract_tool_results(content: Any) -> list[dict[str, Any]]:
    """Pull tool_result blocks from user message content."""
    if not isinstance(content, list):
        return []
    results = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                texts = []
                for rc in result_content:
                    if isinstance(rc, dict) and rc.get("type") == "text":
                        texts.append(rc.get("text", ""))
                result_content = "\n".join(texts)
            results.append({
                "tool_use_id": block.get("tool_use_id", ""),
                "content": result_content,
                "is_error": block.get("is_error", False),
            })
    return results


# ---------------------------------------------------------------------------
# Noise / continuation detection
# ---------------------------------------------------------------------------

_NOISE_PATTERNS = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>/",
    "Unknown skill:",
    "<task-notification>",
    "Full transcript available at:",
    "Read the output file to retrieve the result:",
)


def _is_noise_user_message(text: str) -> bool:
    """Filter out UI noise from user messages."""
    return any(p in text for p in _NOISE_PATTERNS)


def _is_continuation(text: str) -> bool:
    """Is this user message a context-continuation boilerplate?"""
    return "continued from a previous conversation" in text


# ---------------------------------------------------------------------------
# Artifact / figure detection
# ---------------------------------------------------------------------------

def _is_project_md(path: str, project_roots: Iterable[str]) -> bool:
    """Is this path a project .md file (not Claude memory or similar)?"""
    return path.endswith(".md") and any(path.startswith(root) for root in project_roots)


_FIGURE_CMD_PREFIX = re.compile(r"(PYTHONPATH=\S+\s+)?(python3?|set\s)")
_FIGURE_CMD_KEYWORDS = re.compile(r"plot|savefig|pareto|\.png", re.IGNORECASE)


def _is_figure_command(cmd: str) -> bool:
    """Does this Bash command likely produce a figure?"""
    cmd_stripped = cmd.strip()
    if not _FIGURE_CMD_PREFIX.match(cmd_stripped):
        return False
    return bool(_FIGURE_CMD_KEYWORDS.search(cmd_stripped))


# ---------------------------------------------------------------------------
# Raw-record parsing (from a list of .jsonl lines)
# ---------------------------------------------------------------------------

def _parse_lines_to_records(lines: Iterable[str], starting_line_num: int = 0) -> list[dict[str, Any]]:
    """Parse .jsonl lines into a list of raw records with extracted fields.

    Non-user/assistant records (system, tool_use, etc.) are dropped.
    Malformed lines are skipped silently (logged would be better; for v0 we
    prefer robustness to noisy transcripts over verbose logs).
    """
    records = []
    for offset, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        rec_type = d.get("type")
        if rec_type not in ("user", "assistant"):
            continue

        content = d.get("message", {}).get("content", "")
        text = _extract_text(content)
        tool_calls = _extract_tool_calls(content) if rec_type == "assistant" else []
        tool_results = _extract_tool_results(content) if rec_type == "user" else []

        records.append({
            "line": starting_line_num + offset,
            "type": rec_type,
            "timestamp": d.get("timestamp", ""),
            "text": text,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
        })
    return records


# ---------------------------------------------------------------------------
# Record → event conversion
# ---------------------------------------------------------------------------

def _records_to_events(
    records: list[dict[str, Any]],
    project_roots: Iterable[str],
    tool_call_map: dict[str, dict[str, Any]] | None = None,
) -> list[Event]:
    """Convert raw records into a flat list of typed events.

    `tool_call_map` maps tool_use_id → tool call info for matching errors
    back to their originating call. If None, it is built from `records`;
    for incremental parsing, pass in a persistent map to handle errors
    whose originating call appeared in an earlier poll.
    """
    if tool_call_map is None:
        tool_call_map = {}
        for rec in records:
            for tc in rec["tool_calls"]:
                tool_call_map[tc["id"]] = tc

    events: list[Event] = []

    for rec in records:
        ts = rec["timestamp"]

        if rec["type"] == "assistant":
            for tc in rec["tool_calls"]:
                name = tc["name"]
                inp = tc["input"]

                if name == "Write" and _is_project_md(inp.get("file_path", ""), project_roots):
                    events.append({
                        "type": "artifact_write",
                        "timestamp": ts,
                        "path": inp["file_path"],
                        "content": inp.get("content", ""),
                    })
                elif name == "Edit" and _is_project_md(inp.get("file_path", ""), project_roots):
                    events.append({
                        "type": "artifact_edit",
                        "timestamp": ts,
                        "path": inp["file_path"],
                        "old_string": inp.get("old_string", ""),
                        "new_string": inp.get("new_string", ""),
                    })
                elif name == "Bash" and _is_figure_command(inp.get("command", "")):
                    events.append({
                        "type": "figure",
                        "timestamp": ts,
                        "command": inp.get("command", ""),
                    })

            text = rec["text"].strip()
            if text:
                events.append({
                    "type": "assistant_text",
                    "timestamp": ts,
                    "text": text,
                })

        elif rec["type"] == "user":
            for tr in rec["tool_results"]:
                if tr["is_error"]:
                    tc_info = tool_call_map.get(tr["tool_use_id"], {})
                    events.append({
                        "type": "tool_error",
                        "timestamp": ts,
                        "tool_name": tc_info.get("name", "?"),
                        "error": (tr["content"] or "")[:500],
                    })

            text = rec["text"].strip()
            if not text or _is_noise_user_message(text) or _is_continuation(text):
                continue

            events.append({
                "type": "user_text",
                "timestamp": ts,
                "text": text,
            })

    return events


# ---------------------------------------------------------------------------
# Project-root detection
# ---------------------------------------------------------------------------

_YOC_ROOT_RE = re.compile(r"(/home/[^/]+/YoC/[^/]+/)")


def detect_project_roots(records: Iterable[dict[str, Any]]) -> list[str]:
    """Auto-detect project root(s) from Write/Edit paths in the transcript.

    Heuristic: looks for /home/<user>/YoC/<project>/ prefixes. Works for
    Ariel's layout; other layouts can pass `project_roots` explicitly.
    """
    roots: set[str] = set()
    for rec in records:
        for tc in rec.get("tool_calls", []):
            name = tc["name"]
            path = tc["input"].get("file_path", "")
            if name in ("Write", "Edit") and path.endswith(".md"):
                m = _YOC_ROOT_RE.match(path)
                if m and ".claude" not in path:
                    roots.add(m.group(1))
    return sorted(roots)


# ---------------------------------------------------------------------------
# Offline entry point (whole-file parse)
# ---------------------------------------------------------------------------

def parse_whole_file(
    path: str | Path,
    project_roots: list[str] | None = None,
) -> tuple[list[Event], list[str]]:
    """Parse an entire .jsonl transcript file.

    Returns (events, project_roots). `project_roots` is either the
    provided list or the auto-detected one.
    """
    path = Path(path)
    with open(path) as f:
        lines = f.readlines()
    records = _parse_lines_to_records(lines)
    if project_roots is None:
        project_roots = detect_project_roots(records)
    events = _records_to_events(records, project_roots)
    return events, project_roots


# ---------------------------------------------------------------------------
# Online entry point (incremental poll)
# ---------------------------------------------------------------------------

@dataclass
class ParserState:
    """Incremental parser state. Pass into each poll_new_events call.

    Tracks:
      - `line_offset`: how many lines of the transcript we've already
        consumed (next poll reads from here).
      - `project_roots`: detected once (on first poll) and reused; avoids
        re-running detection each tick.
      - `tool_call_map`: accumulates tool calls across polls so that a
        tool error arriving in poll N can be matched to a call from
        poll N-k.
    """
    line_offset: int = 0
    project_roots: list[str] = field(default_factory=list)
    tool_call_map: dict[str, dict[str, Any]] = field(default_factory=dict)


def poll_new_events(
    path: str | Path,
    state: ParserState,
) -> tuple[list[Event], ParserState]:
    """Read any new lines from the transcript since the last poll.

    Returns (new_events, updated_state). If the file hasn't grown, returns
    ([], state) with state unchanged.

    Claude Code .jsonl transcripts are append-only, so a simple line-count
    watermark is sufficient. If the file is truncated or replaced (new
    session), callers should construct a fresh ParserState.
    """
    path = Path(path)
    if not path.exists():
        return [], state

    with open(path) as f:
        all_lines = f.readlines()

    if len(all_lines) <= state.line_offset:
        return [], state

    new_lines = all_lines[state.line_offset:]
    new_records = _parse_lines_to_records(new_lines, starting_line_num=state.line_offset)

    # First-time call: detect project roots from *all* records so far.
    # Subsequent calls: reuse the detection; new project roots are rare
    # (one per session, established early).
    if not state.project_roots:
        # Also look at records we've already processed plus these new ones.
        # Simplest: re-read the whole file for root detection on first call.
        full_records = _parse_lines_to_records(all_lines)
        project_roots = detect_project_roots(full_records)
    else:
        project_roots = state.project_roots

    # Update tool call map with any new calls.
    updated_tool_map = dict(state.tool_call_map)
    for rec in new_records:
        for tc in rec["tool_calls"]:
            updated_tool_map[tc["id"]] = tc

    events = _records_to_events(new_records, project_roots, tool_call_map=updated_tool_map)

    updated_state = ParserState(
        line_offset=len(all_lines),
        project_roots=project_roots,
        tool_call_map=updated_tool_map,
    )
    return events, updated_state
