"""Tests for `hunch init` — project scaffold + hook merge."""

from __future__ import annotations

import json

import pytest

from hunch.cli import main as cli_main
from hunch.init import STOP_HOOK_COMMAND, UPS_HOOK_COMMAND, init_project


# ---------------------------------------------------------------------------
# init_project — happy paths
# ---------------------------------------------------------------------------

def test_init_fresh_project_creates_replay_dir(tmp_path):
    result = init_project(tmp_path)
    assert result.replay_dir_created is True
    assert (tmp_path / ".hunch" / "replay").is_dir()


def test_init_fresh_project_creates_settings_file(tmp_path):
    result = init_project(tmp_path)
    assert result.settings_file_created is True
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    ups_cmds = _all_hook_commands(settings, "UserPromptSubmit")
    assert UPS_HOOK_COMMAND in ups_cmds
    stop_cmds = _all_hook_commands(settings, "Stop")
    assert STOP_HOOK_COMMAND in stop_cmds


def test_init_preserves_existing_unrelated_settings(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "env": {"MY_VAR": "yes"},
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hello"}]}
            ]
        }
    }))

    init_project(tmp_path)

    settings = json.loads(settings_path.read_text())
    assert settings["env"] == {"MY_VAR": "yes"}
    assert settings["permissions"] == {"allow": ["Bash"]}
    # SessionStart hook still there.
    session_cmds = _all_hook_commands(settings, "SessionStart")
    assert "echo hello" in session_cmds
    # UserPromptSubmit now added.
    ups_cmds = _all_hook_commands(settings, "UserPromptSubmit")
    assert UPS_HOOK_COMMAND in ups_cmds


def test_init_coexists_with_other_userpromptsubmit_hook(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "some_other_tool"}]}
            ]
        }
    }))

    init_project(tmp_path)

    cmds = _all_hook_commands(
        json.loads(settings_path.read_text()), "UserPromptSubmit"
    )
    assert "some_other_tool" in cmds
    assert UPS_HOOK_COMMAND in cmds


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_init_twice_is_idempotent(tmp_path):
    init_project(tmp_path)
    result2 = init_project(tmp_path)
    assert result2.already_initialized is True
    assert result2.hooks_added == []
    # Still only one hunch hook entry per type.
    settings = json.loads(
        (tmp_path / ".claude" / "settings.local.json").read_text()
    )
    ups_cmds = _all_hook_commands(settings, "UserPromptSubmit")
    assert ups_cmds.count(UPS_HOOK_COMMAND) == 1
    stop_cmds = _all_hook_commands(settings, "Stop")
    assert stop_cmds.count(STOP_HOOK_COMMAND) == 1


def test_init_second_run_preserves_edited_adjacent_hook(tmp_path):
    init_project(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text())
    settings["hooks"].setdefault("PostToolUse", []).append(
        {"matcher": "Edit",
         "hooks": [{"type": "command", "command": "mark_edited.sh"}]}
    )
    settings_path.write_text(json.dumps(settings))

    init_project(tmp_path)

    final = json.loads(settings_path.read_text())
    # Our edit is still there.
    post = _all_hook_commands(final, "PostToolUse")
    assert "mark_edited.sh" in post


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_init_refuses_invalid_json_settings_file(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{ not valid json")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        init_project(tmp_path)


def test_init_refuses_hooks_that_is_not_an_object(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": "not-an-object"}))
    with pytest.raises(RuntimeError, match="not an object"):
        init_project(tmp_path)


def test_init_refuses_userpromptsubmit_that_is_not_an_array(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {"UserPromptSubmit": "not-an-array"}
    }))
    with pytest.raises(RuntimeError, match="not an array"):
        init_project(tmp_path)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

def test_cli_init_with_explicit_cwd(tmp_path, capsys):
    rc = cli_main(["init", "--cwd", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(tmp_path) in out
    assert (tmp_path / ".hunch" / "replay").is_dir()
    assert (tmp_path / ".claude" / "settings.local.json").exists()


def test_cli_init_missing_directory(tmp_path, capsys):
    rc = cli_main(["init", "--cwd", str(tmp_path / "nope")])
    assert rc == 1
    assert "not a directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_hook_commands(settings: dict, event: str) -> list[str]:
    cmds = []
    for group in settings.get("hooks", {}).get(event, []):
        for hook in group.get("hooks", []):
            if hook.get("type") == "command":
                cmds.append(hook.get("command", ""))
    return cmds
