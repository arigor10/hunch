"""Tests for `hunch start` — the tmux working-layout launcher."""

from __future__ import annotations

from hunch.cli import main as cli_main
from hunch.start import (
    _manual_instructions,
    _new_session_commands,
    _other_research_is_live,
    _parse_roles,
    _run_command,
    start,
)


def test_run_command_default_and_config():
    assert _run_command(None) == "hunch run"
    assert _run_command("configs/x.toml") == "hunch run --config configs/x.toml"


def test_new_session_commands_layout():
    cmds = _new_session_commands("/proj", "hunch run")
    # new session launches the (resumed) research agent in the left pane
    assert cmds[0][:3] == ["tmux", "new-session", "-d"]
    assert cmds[0][-1] == "claude --continue || claude"
    # research kept wide: right column sized at 35% via -l (not the removed -p)
    assert "-l" in cmds[1] and "35%" in cmds[1]
    assert "-p" not in cmds[1]
    assert cmds[1][-1] == "hunch panel"
    assert cmds[2][1:3] == ["split-window", "-v"]
    assert cmds[2][-1] == "hunch run"
    # the three panes are tagged with their roles (for idempotent re-runs)
    tagged = {c[-1] for c in cmds if c[1] == "set-option"}
    assert tagged == {"research", "panel", "run"}
    # research (left) pane is focused at the end
    assert cmds[-1][1] == "select-pane"


def test_parse_roles_skips_untagged():
    out = "%1 research\n%2 panel\n%3 \n%4 run\n"
    assert _parse_roles(out) == {"research": "%1", "panel": "%2", "run": "%4"}


def test_other_research_is_live():
    # no research pane, or it's the current pane → nothing live "elsewhere"
    assert _other_research_is_live(None, "%9", None) is False
    assert _other_research_is_live("%9", "%9", "python3") is False  # current pane, skipped
    # a different research pane running Claude (node) → live elsewhere
    assert _other_research_is_live("%1", "%9", "node") is True
    assert _other_research_is_live("%1", "%9", "claude") is True
    # a different research pane that's just an idle shell → not live
    assert _other_research_is_live("%1", "%9", "bash") is False


def test_manual_instructions_lists_all_three():
    txt = _manual_instructions("hunch run")
    assert "claude" in txt
    assert "hunch panel" in txt
    assert "hunch run" in txt


def test_start_guard_when_not_initialized(tmp_path, capsys):
    assert start(tmp_path) == 1
    assert "not set up" in capsys.readouterr().err


def test_start_falls_back_when_no_tmux(tmp_path, monkeypatch, capsys):
    (tmp_path / ".hunch" / "replay").mkdir(parents=True)
    monkeypatch.setattr("hunch.start.shutil.which", lambda _: None)
    assert start(tmp_path) == 0
    out = capsys.readouterr().out
    assert "tmux not found" in out
    assert "hunch panel" in out


def test_cli_start_guard_on_uninitialized(tmp_path, capsys):
    assert cli_main(["start", "--cwd", str(tmp_path)]) == 1
    assert "not set up" in capsys.readouterr().err
