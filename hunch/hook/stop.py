"""Stop hook handler.

Claude Code fires the Stop hook when Claude finishes a turn. The hook
appends a synthetic `claude_stopped` event to conversation.jsonl so the
framework loop can fire the Critic immediately — hunches are ready
before the user types their next message.

The event carries a `tick_seq` one higher than the last line in
conversation.jsonl. There's a tiny race window (another writer could
append between our read and write), but in practice only one hook fires
at a time and the framework loop is the only other writer.

Contract:
  - Read the last line of conversation.jsonl to get current tick_seq.
  - Append {"type": "claude_stopped", "tick_seq": N+1, "timestamp": ...}.
  - Never crash Claude Code. If anything goes wrong, exit silently.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

from hunch.journal.append import append_json_line


def handle_stop(
    replay_dir: Path,
    now_iso: str | None = None,
) -> int:
    """Append a claude_stopped event to conversation.jsonl.

    Returns 0 on success, 0 on any error (never crash Claude Code).
    """
    try:
        conversation_path = replay_dir / "conversation.jsonl"
        if not conversation_path.exists():
            return 0

        tick_seq = _last_tick_seq(conversation_path)
        if tick_seq is None:
            return 0

        ts = now_iso or _utc_now_iso()
        event = {
            "tick_seq": tick_seq + 1,
            "type": "claude_stopped",
            "timestamp": ts,
        }
        append_json_line(conversation_path, event)
        return 0
    except Exception:
        return 0


def _last_tick_seq(path: Path) -> int | None:
    """Read the last non-empty line of a JSONL file and extract tick_seq."""
    last_line = None
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        if pos == 0:
            return None
        buf = b""
        while pos > 0:
            chunk_size = min(4096, pos)
            pos -= chunk_size
            f.seek(pos)
            buf = f.read(chunk_size) + buf
            lines = buf.split(b"\n")
            for line in reversed(lines):
                line = line.strip()
                if line:
                    last_line = line
                    break
            if last_line is not None:
                break
    if last_line is None:
        return None
    try:
        entry = json.loads(last_line)
        return int(entry["tick_seq"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    """Invoked as `hunch hook stop`."""
    import argparse

    parser = argparse.ArgumentParser(prog="hunch hook stop")
    parser.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )
    ns = parser.parse_args(argv)

    replay_dir = ns.replay_dir or (Path.cwd() / ".hunch" / "replay")
    return handle_stop(replay_dir)
