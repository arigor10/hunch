"""Tmux relay — deliver approved hunches into a parked Claude pane.

When the Scientist approves a hunch while Claude is sitting idle (parked at the
prompt), neither hook fires on its own — the Stop hook only fires at the instant
Claude finishes a turn, and UPS only fires on the next message. So the panel
relays: it types the hunch straight into the tagged research pane.

Safety rests on three things:

  - **Only when parked.** We type only if the replay buffer's last event is
    ``claude_stopped`` (Claude finished a turn and is waiting at the prompt).
    Typing into a busy Claude is the failure we're avoiding.
  - **Mark before send.** We mark the hunches ``surfaced`` *before* typing, so
    the UserPromptSubmit hook — which fires on the relayed message — sees them
    already surfaced and doesn't double-inject.
  - **Roll back on failure.** If the tmux send fails, we move the status back to
    ``pending`` so a failed relay is never left recorded as delivered. The typed
    message itself is the delivery vehicle; a successful send into the tagged,
    parked pane is the evidence.

Outcomes are returned (not raised) so the panel can give precise feedback and
tests can assert on the decision. The function never raises.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hunch.hook.delivery import (
    _utc_now_iso,
    find_approved,
    format_hunch_injection,
    mark_status,
)
from hunch.journal.feedback import read_hunch_edits
from hunch.tmux import (
    RelayError,
    current_pane_id,
    in_tmux,
    send_text_to_pane,
    window_roles,
)

# Outcome constants.
RELAYED = "relayed"
NOT_IN_TMUX = "not_in_tmux"
NO_RESEARCH_PANE = "no_research_pane"
NOT_PARKED = "not_parked"
NOTHING_TO_DELIVER = "nothing_to_deliver"
FAILED = "failed"


def relay_pending(replay_dir: Path, now_iso: str | None = None) -> str:
    """Deliver all currently-approved hunches into the parked research pane.

    Returns one of the outcome constants above. Never raises — on an unexpected
    error it logs to stderr and returns ``FAILED`` without leaving a hunch
    falsely marked surfaced (best effort).
    """
    try:
        if not in_tmux():
            return NOT_IN_TMUX

        roles = window_roles()
        research = roles.get("research")
        if not research or research == current_pane_id():
            # No tagged research pane, or we'd be typing into ourselves.
            return NO_RESEARCH_PANE

        if not _claude_parked(replay_dir):
            # Claude is mid-turn; the Stop hook will deliver at turn-end.
            return NOT_PARKED

        approved = find_approved(replay_dir)
        if not approved:
            return NOTHING_TO_DELIVER

        edits = read_hunch_edits(replay_dir / "feedback.jsonl")
        text = format_hunch_injection(approved, edits=edits)
        ts = now_iso or _utc_now_iso()

        # Mark surfaced BEFORE sending: the relayed message triggers the UPS
        # hook, which must see these as already delivered and skip them.
        mark_status(replay_dir, approved, "surfaced", by="panel:relay", now_iso=ts)
        try:
            send_text_to_pane(research, text)
        except RelayError as exc:
            # Roll back — a failed relay must not be recorded as delivered. The
            # hunch stays pending and is carried by UPS on the next message.
            mark_status(
                replay_dir, approved, "pending",
                by="panel:relay-failed", now_iso=ts,
            )
            print(
                f"[hunch relay] send failed, rolled back to pending: {exc}",
                file=sys.stderr,
            )
            return FAILED
        return RELAYED
    except Exception as exc:  # never crash the panel
        print(f"[hunch relay] unexpected error: {exc}", file=sys.stderr)
        return FAILED


# Turn boundaries in the replay buffer. A turn-opener (a user message, or a
# hunch we injected/relayed that Claude then works on) starts a turn;
# claude_stopped closes it.
_STOP_EVENT = "claude_stopped"
_TURN_OPENERS = {"user_text", "hunch_injection"}


def _claude_parked(replay_dir: Path) -> bool:
    """True if Claude finished its last turn and is waiting at the prompt.

    We do NOT require ``claude_stopped`` to be the literal last line: in a live
    ``hunch run`` session the ingest appends trailing output (``assistant_text``,
    ``artifact_edit``) from the just-finished turn *after* the Stop hook writes
    ``claude_stopped``. Instead we ask whether the most recent *turn boundary* is
    a stop rather than an opener — scanning from the end, the first boundary
    event decides it. That's immune to trailing-output lag and to the live
    tick-numbering collapse (it keys on event type + file order, not tick_seq).

    Crucially, a ``hunch_injection`` counts as an opener: if Claude is still
    working through a hunch we *just* relayed (or the Stop hook just delivered),
    a second approval must NOT relay into that busy turn — it waits for the Stop
    hook to deliver at turn-end instead.
    """
    conversation_path = replay_dir / "conversation.jsonl"
    if not conversation_path.exists():
        return False
    for entry in _iter_events_reversed(conversation_path):
        t = entry.get("type")
        if t == _STOP_EVENT:
            return True
        if t in _TURN_OPENERS:
            return False
    return False


def _iter_events_reversed(conversation_path: Path):
    """Yield parsed JSON events from a JSONL file, last line first. Malformed
    lines are skipped."""
    with open(conversation_path, encoding="utf-8") as f:
        lines = f.readlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
