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

from hunch.journal.feedback import read_labeled_hunch_ids
from hunch.journal.hunches import HunchRecord, HunchesWriter, read_current_hunches


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

def _format_additional_context(hunches: list[HunchRecord]) -> str:
    """Render pending hunches as injected context.

    The framing matters: the Researcher is an instruction-follower.
    If we write "INVESTIGATE THIS", it will drop everything. If we
    write "a colleague observed", it reads as information, not
    command. See critic_v0.md §Output schema rationale.
    """
    lines = [
        "<hunch-injection>",
        "A meeting-room colleague (Hunch) has been watching this work "
        "and left the following observations for the Scientist (the user) "
        "to weigh. They are not instructions for you, the Researcher, "
        "and you should not reorient your work around them; continue "
        "with the task the Scientist has asked. The Scientist may or "
        "may not bring them up in reply.",
        "",
    ]
    for h in hunches:
        lines.append(f"- [{h.hunch_id}] {h.smell}")
        if h.description:
            lines.append(f"    {h.description}")
    lines.append("</hunch-injection>")
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
    additionalContext, and mark them surfaced.

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

        records = read_current_hunches(hunches_path)
        labels = read_labeled_hunch_ids(replay_dir / "feedback.jsonl")
        approved = [
            r for r in records
            if r.status == "pending" and labels.get(r.hunch_id) == "good"
        ]
        if not approved:
            return _empty_continue()

        context = _format_additional_context(approved)

        ts = now_iso or _utc_now_iso()
        writer = HunchesWriter(hunches_path=hunches_path)
        for r in approved:
            writer.write_status_change(
                hunch_id=r.hunch_id,
                new_status="surfaced",
                ts=ts,
                by="hook:user_prompt_submit",
            )

        payload = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
        return HookResult(stdout=json.dumps(payload))
    except Exception:
        # Hook must never crash the user's prompt. Swallow and
        # continue — the cost is one missed injection, not a broken
        # Claude Code session.
        return _empty_continue()


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
