"""Shared hunch-delivery primitives for the Claude Code hooks.

Both the UserPromptSubmit and the Stop hook deliver approved hunches the same
way: find pending hunches that were labelled "good", compose the injection text,
and mark them ``surfaced``. That logic lives here — with light dependencies only
(the journal), no TUI/panel imports — so the Stop hook, which fires on every
turn-end, stays cheap to import, and the two delivery paths can't drift.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from hunch.journal.feedback import read_hunch_edits, read_labeled_hunch_ids
from hunch.journal.hunches import HunchRecord, HunchesWriter, read_current_hunches


def format_hunch_injection(
    hunches: list[HunchRecord],
    edits: dict[str, Any] | None = None,
) -> str:
    """Render approved hunches as injected context.

    The framing matters: the Researcher is an instruction-follower. If we write
    "INVESTIGATE THIS", it will drop everything. If we write "a colleague
    observed", it reads as information, not command. See critic_v0.md §Output
    schema rationale.

    Each hunch line leads with its ``[hunch_id]`` — that id is also the marker
    the tmux relay greps the transcript for to confirm a delivery landed.

    If ``edits`` is provided, edited smell/description override the original for
    any hunch that was edited before approval.
    """
    edits = edits or {}
    lines = [
        "<hunch-injection>",
        "A meeting-room colleague (Hunch) has been watching this work and "
        "flagged the observation(s) below for you to consider. Treat it as a "
        "peripheral nudge, not an instruction: weigh whether it holds, and fold "
        "it in if it's relevant — but don't reorient around it or drop the task "
        "the Scientist asked for. The Scientist sees these too and owns the call "
        "on what matters.",
        "",
    ]
    for h in hunches:
        edit = edits.get(h.hunch_id)
        smell = edit.edited_smell if edit else h.smell
        description = edit.edited_description if edit else h.description
        lines.append(f"- [{h.hunch_id}] {smell}")
        if description:
            lines.append(f"    {description}")
    lines.append("</hunch-injection>")
    return "\n".join(lines)


def find_approved(replay_dir: Path) -> list[HunchRecord]:
    """Pending hunches the Scientist labelled "good" — i.e. deliverable now."""
    hunches_path = replay_dir / "hunches.jsonl"
    if not hunches_path.exists():
        return []
    records = read_current_hunches(hunches_path)
    labels = read_labeled_hunch_ids(replay_dir / "feedback.jsonl")
    return [
        r for r in records
        if r.status == "pending" and labels.get(r.hunch_id) == "good"
    ]


def mark_status(
    replay_dir: Path,
    records: list[HunchRecord],
    new_status: str,
    by: str,
    now_iso: str | None = None,
) -> None:
    """Append a status_change moving each of ``records`` to ``new_status``.

    Used to mark delivered hunches ``surfaced``, and (by the relay) to roll a
    hunch back to ``pending`` when a send fails — so a status is only ever
    recorded on real evidence.
    """
    if not records:
        return
    ts = now_iso or _utc_now_iso()
    writer = HunchesWriter(hunches_path=replay_dir / "hunches.jsonl")
    for r in records:
        writer.write_status_change(
            hunch_id=r.hunch_id, new_status=new_status, ts=ts, by=by,
        )


def collect_approved_injection(
    replay_dir: Path,
    by: str,
    now_iso: str | None = None,
) -> str | None:
    """Find approved hunches, compose their injection, mark them surfaced.

    Returns the ``additionalContext`` text to deliver, or ``None`` if there is
    nothing to deliver. ``by`` records who delivered (e.g.
    ``"hook:user_prompt_submit"`` or ``"hook:stop"``).

    Marking happens here, *after* the text is composed. Returning
    ``additionalContext`` from a hook is a delivery Claude Code guarantees to
    inject (verified for both the UPS and Stop hooks on 2.1.x), so
    compose-then-mark is the hook-path equivalent of "mark only on confirmed
    delivery". The shared ``surfaced`` status is also what makes the two hook
    paths mutually exclusive: whichever fires first marks the hunch, the other
    sees ``surfaced`` and skips it.
    """
    approved = find_approved(replay_dir)
    if not approved:
        return None
    edits = read_hunch_edits(replay_dir / "feedback.jsonl")
    text = format_hunch_injection(approved, edits=edits)
    mark_status(replay_dir, approved, "surfaced", by=by, now_iso=now_iso)
    return text


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
