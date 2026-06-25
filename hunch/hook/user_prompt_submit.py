"""UserPromptSubmit hook handler.

Claude Code fires UserPromptSubmit when the Scientist sends a prompt.
The hook receives the tool input on stdin (JSON with `prompt`,
`session_id`, `cwd`, …) and returns structured JSON on stdout. The
`hookSpecificOutput.additionalContext` field is appended to the
Researcher's system prompt for this one turn — our injection point.

Contract we implement:

  - Read `hunches.jsonl`, fold to current state (pending hunches only).
  - If there are any, emit them as `additionalContext` formatted
    for the Researcher to notice but not be commanded by.
  - For each surfaced hunch, append a `status_change` event
    transitioning `pending` → `surfaced`. That closes the loop:
    the same hunch won't re-surface on the next prompt.
  - Never crash Claude Code. If anything goes wrong (missing replay
    dir, malformed jsonl, anything), emit an empty-continue response.
    The hook must be invisible when Hunch is misconfigured.

The handler is pure-ish: it takes stdin bytes + a replay dir and
returns a (stdout_bytes, exit_code) pair. `main()` is the thin
argv/stdio wrapper for the CLI.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunch.hook.delivery import collect_approved_injection
from hunch.journal.feedback import (
    FeedbackWriter,
    read_hunch_edits,
    read_hunch_reminder_counts,
    read_hunch_reminders,
    read_hunch_responses,
)
from hunch.journal.hunches import HunchRecord, read_current_hunches
from hunch.panel import read_max_tick_seq


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HookResult:
    """What the handler returns to its caller.

    `stdout` is the JSON payload Claude Code will read. `exit_code` is
    0 unless we want to signal a hard failure — but we almost never
    do, because crashing the hook crashes the user's prompt.
    """
    stdout: str
    exit_code: int = 0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

REMINDER_INTERVAL_TURNS = 10
MAX_REMINDERS = 2  # after this many nudges, stop — a missed acknowledgment
                   # (e.g. a "Re h-XXXX" line the parser couldn't match) must
                   # not nag the Researcher forever.


def format_hunch_reminder(
    hunches: list[HunchRecord],
    edits: dict[str, Any] | None = None,
) -> str:
    """Render a reminder for surfaced-but-unacknowledged hunches.

    Softer framing than ``format_hunch_injection`` — not a new delivery,
    just a nudge to respond when ready, plus context restoration in case
    the original injection was compressed away.
    """
    edits = edits or {}
    lines = [
        "<hunch-reminder>",
        "These hunches were delivered earlier. When you've worked through them,",
        'include a "Re h-XXXX:" line with your conclusion.',
        "",
    ]
    for h in hunches:
        edit = edits.get(h.hunch_id)
        smell = edit.edited_smell if edit else h.smell
        description = edit.edited_description if edit else h.description
        lines.append(f"- [{h.hunch_id}] {smell}")
        if description:
            lines.append(f"    {description}")
    lines.append("</hunch-reminder>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------

def handle_user_prompt_submit(
    stdin_bytes: bytes,
    replay_dir: Path,
    now_iso: str | None = None,
) -> HookResult:
    """Read pending hunches from `replay_dir`, inject them as
    additionalContext, and mark them surfaced. Also remind about
    surfaced-but-unacknowledged hunches.

    `stdin_bytes` is what Claude Code sent — currently unused for
    injection (we always inject pending hunches regardless of prompt
    content), but accepted and parsed defensively so we can log
    session_id etc. in future.

    `now_iso` is an injectable clock so tests don't need to patch
    datetime. Callers pass `None` to use real time.

    Never raises. Any exception becomes an empty-continue response.
    """
    try:
        hunches_path = replay_dir / "hunches.jsonl"
        if not hunches_path.exists():
            return _empty_continue()

        feedback_path = replay_dir / "feedback.jsonl"
        ts = now_iso or _utc_now_iso()
        context_parts: list[str] = []

        # --- Reminder snapshot, taken BEFORE delivery marks anything surfaced,
        # so a just-approved hunch is delivered (below) rather than immediately
        # reminded about. ---
        records = read_current_hunches(hunches_path)
        edits = read_hunch_edits(feedback_path)
        responses = read_hunch_responses(feedback_path)
        reminders = read_hunch_reminders(feedback_path)
        reminder_counts = read_hunch_reminder_counts(feedback_path)
        max_seq = read_max_tick_seq(replay_dir / "conversation.jsonl")

        surfaced_unacked = [
            r for r in records
            if r.status == "surfaced" and r.hunch_id not in responses
        ]
        due_for_reminder = [
            r for r in surfaced_unacked
            if _reminder_due(r.hunch_id, reminders, reminder_counts, max_seq)
        ]

        # --- Deliver newly-approved hunches (shared with the Stop hook). This
        # marks them surfaced, which is why the reminder snapshot is read first. ---
        injection = collect_approved_injection(
            replay_dir, by="hook:user_prompt_submit", now_iso=ts
        )
        if injection is not None:
            context_parts.append(injection)

        if due_for_reminder:
            context_parts.append(format_hunch_reminder(due_for_reminder, edits=edits))
            fw = FeedbackWriter(feedback_path=feedback_path)
            for r in due_for_reminder:
                fw.write_reminder(hunch_id=r.hunch_id, ts=ts, tick_seq=max_seq)

        if not context_parts:
            return _empty_continue()

        payload = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(context_parts),
            }
        }
        return HookResult(stdout=json.dumps(payload))
    except Exception as exc:
        import sys
        print(f"[hunch prompt hook] error: {exc}", file=sys.stderr)
        return _empty_continue()


def _reminder_due(
    hunch_id: str,
    reminders: dict[str, int],
    reminder_counts: dict[str, int],
    current_seq: int,
) -> bool:
    """Check if a surfaced-but-unacknowledged hunch is due for a reminder.

    Due if under the reminder cap AND (never reminded, or
    REMINDER_INTERVAL_TURNS have passed since the last reminder). The cap bounds
    the damage of an unrecorded acknowledgment: we nudge a few times, then leave
    the hunch surfaced-but-quiet rather than looping forever.
    """
    if reminder_counts.get(hunch_id, 0) >= MAX_REMINDERS:
        return False
    last_seq = reminders.get(hunch_id)
    if last_seq is None:
        return True
    return (current_seq - last_seq) >= REMINDER_INTERVAL_TURNS


def _empty_continue() -> HookResult:
    """No-op response: Claude Code proceeds with the prompt unchanged."""
    return HookResult(stdout="")


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CLI glue
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Invoked as `hunch hook user-prompt-submit`.

    Resolves the replay dir from the cwd (same convention as `hunch
    run`), reads stdin, delegates to `handle_user_prompt_submit`,
    writes stdout, returns the handler's exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="hunch hook user-prompt-submit")
    parser.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )
    ns = parser.parse_args(argv)

    replay_dir = ns.replay_dir or (Path.cwd() / ".hunch" / "replay")
    stdin_bytes = sys.stdin.buffer.read()
    result = handle_user_prompt_submit(stdin_bytes, replay_dir)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return result.exit_code
