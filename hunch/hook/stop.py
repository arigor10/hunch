"""Stop hook handler — deliver-or-rest.

Claude Code fires the Stop hook when Claude finishes a turn. The hook makes one
decision:

  - If an approved hunch is waiting, deliver it as ``additionalContext``. Per
    Claude Code v2.1.152 this keeps the turn going, so Claude reacts to the
    hunch without the user typing — and we deliberately do NOT append
    ``claude_stopped``, because Claude is continuing, not resting.
  - Otherwise, append a synthetic ``claude_stopped`` event to
    conversation.jsonl. That fires the Critic before the user's next message,
    and marks the point where Claude has genuinely parked — the signal the
    tmux relay uses to know it's safe to type.

Delivery (and the ``surfaced`` marking) goes through the shared
``collect_approved_injection`` helper, so the UPS and Stop paths can't
double-deliver: whichever fires first marks the hunch, the other skips it. The
continue-loop is self-bounding — each delivery consumes its hunch (marks it
surfaced); and if marking ever fails, the hook returns no ``additionalContext``
so the turn simply ends rather than looping. ``stop_hook_active`` is available
in the hook's stdin if a further guard is ever needed.

Contract:
  - Never crash Claude Code. On any error, log to stderr and return an empty
    (rest) response.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from hunch.hook.delivery import collect_approved_injection
from hunch.journal.append import append_json_line, read_last_json_line


@dataclass(frozen=True)
class StopResult:
    """What the handler returns. ``stdout`` is the JSON Claude Code reads —
    a ``hookSpecificOutput.additionalContext`` payload when delivering, or the
    empty string when Claude is allowed to rest."""
    stdout: str


def handle_stop(replay_dir: Path, now_iso: str | None = None) -> StopResult:
    """Deliver a waiting hunch (keeping the turn going) or record that Claude
    has come to rest. Never raises."""
    try:
        injection = collect_approved_injection(
            replay_dir, by="hook:stop", now_iso=now_iso
        )
        if injection is not None:
            payload = {
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": injection,
                }
            }
            return StopResult(stdout=json.dumps(payload))

        _append_claude_stopped(replay_dir, now_iso)
        return StopResult(stdout="")
    except Exception as exc:
        print(f"[hunch stop hook] error: {exc}", file=sys.stderr)
        return StopResult(stdout="")


def _append_claude_stopped(replay_dir: Path, now_iso: str | None) -> None:
    """Append a ``claude_stopped`` event to conversation.jsonl.

    The event carries a ``tick_seq`` one higher than the last line. There's a
    tiny race window (another writer could append between our read and write),
    but in practice only one hook fires at a time and the framework loop is the
    only other writer.
    """
    conversation_path = replay_dir / "conversation.jsonl"
    if not conversation_path.exists():
        return
    tick_seq = _last_tick_seq(conversation_path)
    if tick_seq is None:
        return
    ts = now_iso or _utc_now_iso()
    append_json_line(
        conversation_path,
        {"tick_seq": tick_seq + 1, "type": "claude_stopped", "timestamp": ts},
    )


def _last_tick_seq(path: Path) -> int | None:
    """Return the tick_seq of the last event in a JSONL file, or None."""
    entry = read_last_json_line(path)
    if entry is None:
        return None
    try:
        return int(entry["tick_seq"])
    except (KeyError, TypeError, ValueError):
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
    result = handle_stop(replay_dir)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return 0
