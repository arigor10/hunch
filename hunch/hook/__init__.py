"""Claude Code hook handlers.

Hunch installs three hooks in the target project's
`.claude/settings.local.json` (gitignored, per-user):

  - `UserPromptSubmit`: fires when the Scientist presses Enter.
    Injects pending hunches as additionalContext.
  - `Stop` (sync): fires when Claude finishes a turn. Appends a
    `claude_stopped` event to conversation.jsonl so the framework
    loop can fire the Critic before the user's next message.
  - `Stop` (asyncRewake): runs in the background after each Claude
    response, polling for approved hunches. Delivers them via
    stderr + exit code 2 so Claude wakes up without a user message.

The CLI (`hunch hook <name>`) dispatches argv into them; all real
logic lives here so it can be unit-tested without shelling out.
"""

from hunch.hook.stop import handle_stop
from hunch.hook.async_delivery import handle_stop_delivery
from hunch.hook.user_prompt_submit import handle_user_prompt_submit

__all__ = ["handle_stop", "handle_stop_delivery", "handle_user_prompt_submit"]
