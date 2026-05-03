"""Render current_block.md for the wiki critic.

Reads replay buffer events in a bookmark range and renders them as
human-readable markdown that the critic agent processes each tick.
"""

from __future__ import annotations

import json
from pathlib import Path


MAX_ARTIFACT_CHARS = 10_000
MAX_DIFF_CHARS = 200


def read_events_in_range(
    conversation_path: Path,
    bookmark_prev: int,
    bookmark_now: int,
) -> list[dict]:
    """Read events where bookmark_prev < tick_seq <= bookmark_now."""
    events: list[dict] = []
    with open(conversation_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            seq = d.get("tick_seq", 0)
            if seq <= bookmark_prev:
                continue
            if seq > bookmark_now:
                break
            events.append(d)
    return events


def render_current_block(
    events: list[dict],
    tick_id: str,
    bookmark_prev: int,
    bookmark_now: int,
    artifacts_dir: Path,
) -> str:
    """Render a list of replay events as a markdown conversation block."""
    if not events:
        return (
            f"# Conversation Block ({tick_id})\n\n"
            f"Replay turns {bookmark_prev + 1}-{bookmark_now}\n\n"
            "---\n\n"
            "(no events in this block)\n"
        )

    timestamps = [e.get("timestamp", "") for e in events if e.get("timestamp")]
    ts_start = timestamps[0] if timestamps else ""
    ts_end = timestamps[-1] if timestamps else ""
    ts_range = f" | {_fmt_ts(ts_start)} \u2013 {_fmt_ts(ts_end)}" if ts_start else ""

    parts: list[str] = [
        f"# Conversation Block ({tick_id})\n",
        f"Replay turns {bookmark_prev + 1}-{bookmark_now}{ts_range}\n",
        "---\n",
    ]

    for event in events:
        rendered = _render_event(event, artifacts_dir)
        if rendered:
            parts.append(rendered)

    return "\n".join(parts) + "\n"


def _render_event(event: dict, artifacts_dir: Path) -> str | None:
    etype = event.get("type", "")

    if etype == "user_text":
        text = event.get("text", "").strip()
        return f"**USER:** {text}\n" if text else None

    if etype == "assistant_text":
        text = event.get("text", "").strip()
        return f"**CLAUDE:** {text}\n" if text else None

    if etype == "artifact_write":
        path = event.get("path", "?")
        snapshot = event.get("snapshot", "")
        content = _read_snapshot(artifacts_dir, snapshot)
        if content is None:
            return f"[ARTIFACT WRITE: {path}]\n[Content unavailable]\n"
        char_count = len(content)
        if char_count > MAX_ARTIFACT_CHARS:
            content = content[:MAX_ARTIFACT_CHARS] + f"\n[... truncated, {char_count} total chars]"
        return (
            f"[ARTIFACT WRITE: {path}]\n"
            f"[Content ({char_count} chars):]\n"
            f"{content}\n"
        )

    if etype == "artifact_edit":
        path = event.get("path", "?")
        skipped = event.get("skipped_reason")
        if skipped:
            return f"[ARTIFACT EDIT (skipped: {skipped}): {path}]\n"
        diff = event.get("diff") or {}
        old = diff.get("old_string", event.get("old_string", ""))
        new = diff.get("new_string", event.get("new_string", ""))
        old_display = _truncate(old, MAX_DIFF_CHARS)
        new_display = _truncate(new, MAX_DIFF_CHARS)
        return f"[ARTIFACT EDIT: {path}]\n[Changed: '{old_display}' -> '{new_display}']\n"

    if etype == "tool_error":
        tool = event.get("tool_name", "unknown")
        error = _truncate(event.get("error", ""), 200)
        return f"[TOOL ERROR ({tool}): {error}]\n"

    if etype == "figure":
        cmd = event.get("command", "")
        return f"[FIGURE: {cmd}]\n" if cmd else None

    return None


def _read_snapshot(artifacts_dir: Path, snapshot_name: str) -> str | None:
    if not snapshot_name:
        return None
    path = artifacts_dir / snapshot_name
    if not path.exists():
        return None
    return path.read_text(errors="replace")


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    return ts.replace("T", " ").split(".")[0].replace("Z", "")
