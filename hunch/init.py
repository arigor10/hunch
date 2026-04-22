"""`hunch init` — set up a project for Hunch.

Three side effects, all idempotent:

  1. Create `<cwd>/.hunch/replay/` so the framework has somewhere to
     write its event-sourced log.
  2. Merge the UserPromptSubmit hook entry into
     `<cwd>/.claude/settings.local.json` so Claude Code will invoke
     `hunch hook user-prompt-submit` on every user prompt, letting
     pending hunches be injected into the Researcher's context.
  3. Merge the Stop hook entry into the same settings file so Claude
     Code will invoke `hunch hook stop` when Claude finishes a turn,
     appending a `claude_stopped` event to the replay buffer.

The merge is deliberately additive: every existing key in the JSON
file is preserved byte-identical. If both hooks are already wired,
`init` is a no-op and says so.

We ship a small install-surface on purpose. Anything more ambitious
(session-start hooks, custom replay-dir location, auto-inject of
env vars) is post-v0 — the first question for Ariel and Paul is
whether the Critic emits anything useful, not how to configure it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UPS_HOOK_COMMAND = "hunch hook user-prompt-submit"
STOP_HOOK_COMMAND = "hunch hook stop"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InitResult:
    """Summary of what `hunch init` changed.

    Everything is a boolean so the CLI can render a terse report and
    tests can assert on specific side effects without scraping stdout.
    """
    replay_dir_created: bool
    settings_file_created: bool
    hooks_added: list[str]
    already_initialized: bool

    def as_lines(self, replay_dir: Path, settings_path: Path) -> list[str]:
        if self.already_initialized:
            return [f"hunch init: already initialized in {settings_path.parent.parent}"]
        lines = []
        if self.replay_dir_created:
            lines.append(f"  created  {replay_dir}")
        else:
            lines.append(f"  existing {replay_dir}")
        if self.settings_file_created:
            lines.append(f"  created  {settings_path}")
        elif self.hooks_added:
            added = ", ".join(self.hooks_added)
            lines.append(f"  updated  {settings_path} (added {added} hook(s))")
        else:
            lines.append(f"  unchanged {settings_path}")
        return lines


def init_project(cwd: Path) -> InitResult:
    """Idempotently set up Hunch for the project rooted at `cwd`.

    Returns an `InitResult` describing what (if anything) was changed.
    """
    cwd = Path(cwd)
    replay_dir = cwd / ".hunch" / "replay"
    settings_path = cwd / ".claude" / "settings.local.json"

    replay_dir_created = not replay_dir.exists()
    replay_dir.mkdir(parents=True, exist_ok=True)

    settings_file_created, hooks_added = _merge_hooks(settings_path)

    already_initialized = (
        not replay_dir_created
        and not settings_file_created
        and not hooks_added
    )

    return InitResult(
        replay_dir_created=replay_dir_created,
        settings_file_created=settings_file_created,
        hooks_added=hooks_added,
        already_initialized=already_initialized,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_HOOKS_TO_REGISTER: list[tuple[str, str]] = [
    ("UserPromptSubmit", UPS_HOOK_COMMAND),
    ("Stop", STOP_HOOK_COMMAND),
]


def _merge_hooks(settings_path: Path) -> tuple[bool, list[str]]:
    """Ensure both hooks are present in the settings file.

    Returns `(file_created, list_of_hook_names_added)`.
    """
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = _minimal_settings()
        _write_settings(settings_path, settings)
        return True, []

    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{settings_path} exists but is not valid JSON ({e}); "
            f"fix by hand and rerun `hunch init`"
        ) from e

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(
            f"{settings_path}: 'hooks' is not an object; "
            f"cannot merge hook entries"
        )

    added: list[str] = []
    for hook_name, command in _HOOKS_TO_REGISTER:
        hook_list = hooks.setdefault(hook_name, [])
        if not isinstance(hook_list, list):
            raise RuntimeError(
                f"{settings_path}: 'hooks.{hook_name}' is not an array"
            )
        if not _hook_already_present(hook_list, command):
            hook_list.append(_hunch_hook_entry(command))
            added.append(hook_name)

    if added:
        _write_settings(settings_path, settings)
    return False, added


def _hook_already_present(hook_list: list[Any], command: str) -> bool:
    """True if any entry in the hook list runs the given command."""
    for group in hook_list:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            if hook.get("type") == "command" and hook.get("command") == command:
                return True
    return False


def _hunch_hook_entry(command: str) -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
            }
        ]
    }


def _minimal_settings() -> dict[str, Any]:
    return {
        "hooks": {
            hook_name: [_hunch_hook_entry(command)]
            for hook_name, command in _HOOKS_TO_REGISTER
        },
    }


def _write_settings(settings_path: Path, settings: dict[str, Any]) -> None:
    """Write settings JSON with 2-space indent (matches Claude Code's style)."""
    text = json.dumps(settings, indent=2) + "\n"
    settings_path.write_text(text)
