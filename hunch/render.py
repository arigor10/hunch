"""Shared rendering for replay buffer events.

Used by both the wiki critic (current_block.md) and the mining
pipeline (chunk rendering).  Every event is labelled with its
tick_seq so consumers can reference specific conversation moments.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


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


def render_events(
    events: list[dict],
    artifacts_dir: Path | None = None,
) -> str:
    """Render a list of replay events as labelled dialogue."""
    parts: list[str] = []
    for event in events:
        rendered = render_event(event, artifacts_dir)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def render_event(event: dict, artifacts_dir: Path | None = None) -> str | None:
    """Render a single replay event as a labelled string."""
    seq = event.get("tick_seq", "?")
    etype = event.get("type", "")

    if etype == "user_text":
        text = event.get("text", "").strip()
        if not text:
            return None
        return f"**USER** (seq {seq}): {text}\n"

    if etype == "assistant_text":
        text = event.get("text", "").strip()
        if not text:
            return None
        return f"**CLAUDE** (seq {seq}): {text}\n"

    if etype == "artifact_write":
        path = event.get("path", "?")
        content = read_snapshot(artifacts_dir, event.get("snapshot", ""))
        if content is None:
            return f"[ARTIFACT WRITE] (seq {seq}): {path}\n[Content unavailable]\n"
        char_count = len(content)
        if char_count > MAX_ARTIFACT_CHARS:
            content = content[:MAX_ARTIFACT_CHARS] + f"\n[... truncated, {char_count} total chars]"
        return (
            f"[ARTIFACT WRITE] (seq {seq}): {path}\n"
            f"[Content ({char_count} chars):]\n"
            f"{content}\n"
        )

    if etype == "artifact_edit":
        path = event.get("path", "?")
        skipped = event.get("skipped_reason")
        if skipped:
            return f"[ARTIFACT EDIT (skipped: {skipped})] (seq {seq}): {path}\n"
        diff = event.get("diff") or {}
        old = diff.get("old_string", event.get("old_string", ""))
        new = diff.get("new_string", event.get("new_string", ""))
        old_d = truncate(old, MAX_DIFF_CHARS)
        new_d = truncate(new, MAX_DIFF_CHARS)
        return f"[ARTIFACT EDIT] (seq {seq}): {path}\n[Changed: '{old_d}' -> '{new_d}']\n"

    if etype == "tool_error":
        tool = event.get("tool_name", "unknown")
        error = truncate(event.get("error", ""), 200)
        return f"[TOOL ERROR ({tool})] (seq {seq}): {error}\n"

    if etype == "figure":
        cmd = event.get("command", "")
        if not cmd:
            return None
        return f"[FIGURE] (seq {seq}): {cmd}\n"

    log.warning("unknown event type '%s' at tick_seq %s, skipping",
                etype, event.get("tick_seq", "?"))
    return None


def read_snapshot(artifacts_dir: Path | None, snapshot_name: str) -> str | None:
    """Read an artifact snapshot file, returning None if unavailable."""
    if not artifacts_dir or not snapshot_name:
        return None
    path = artifacts_dir / snapshot_name
    if not path.exists():
        return None
    return path.read_text(errors="replace")


def truncate(s: str, max_len: int) -> str:
    """Truncate a string, appending '...' if it exceeds max_len."""
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def fmt_ts(ts: str) -> str:
    """Format an ISO timestamp for display (strip T, fractional seconds, Z)."""
    if not ts:
        return ""
    return ts.replace("T", " ").split(".")[0].replace("Z", "")
