"""Claude Code hook handlers.

Hunch installs a `UserPromptSubmit` hook in the target project's
`.claude/settings.local.json` (gitignored, per-user). When the
Scientist presses Enter, Claude Code invokes the hook with the
current tool input on stdin and waits for structured JSON back on
stdout; what we emit in `hookSpecificOutput.additionalContext` is
injected into the Researcher's system prompt for this one turn.

This package holds the hook handlers. The CLI (`hunch hook <name>`)
dispatches argv into them; all real logic lives here so it can be
unit-tested without shelling out.
"""

from hunch.hook.user_prompt_submit import handle_user_prompt_submit

__all__ = ["handle_user_prompt_submit"]
