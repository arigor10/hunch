"""Claude Code hook handlers.

Hunch installs two hooks in the target project's
`.claude/settings.local.json` (gitignored, per-user):

  - `UserPromptSubmit`: fires when the Scientist presses Enter.
    Injects pending approved hunches as additionalContext and marks
    them surfaced.
  - `Stop`: fires when Claude finishes a turn. If an approved hunch is
    waiting, delivers it as additionalContext (keeping the turn going,
    per Claude Code v2.1.152) and marks it surfaced; otherwise appends a
    `claude_stopped` event to conversation.jsonl so the framework loop
    can fire the Critic and the relay can detect that Claude has parked.

The CLI (`hunch hook <name>`) dispatches argv into them; all real
logic lives here so it can be unit-tested without shelling out.
"""

from hunch.hook.stop import handle_stop
from hunch.hook.user_prompt_submit import handle_user_prompt_submit

__all__ = ["handle_stop", "handle_user_prompt_submit"]
