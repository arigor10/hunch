"""Render current_block.md for the wiki critic.

Thin wrapper around ``hunch.render`` that adds the block header
(tick id, bookmark range, timestamps).
"""

from __future__ import annotations

from pathlib import Path

from hunch.render import (  # noqa: F401 — re-exported for existing callers
    fmt_ts,
    read_events_in_range,
    render_event,
    render_events,
)


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
    ts_range = f" | {fmt_ts(ts_start)} \u2013 {fmt_ts(ts_end)}" if ts_start else ""

    header = (
        f"# Conversation Block ({tick_id})\n\n"
        f"Replay turns {bookmark_prev + 1}-{bookmark_now}{ts_range}\n\n"
        "---\n\n"
    )

    body = render_events(events, artifacts_dir)
    return header + body + "\n"
