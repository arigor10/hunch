"""Tests for `hunch start` — the tmux working-layout launcher."""

from __future__ import annotations

from hunch.cli import main as cli_main
from hunch.start import (
    _inside_tmux_commands,
    _manual_instructions,
    _new_session_commands,
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
    # research (left) pane is focused at the end
    assert cmds[-1][1] == "select-pane"


def test_inside_tmux_commands_keep_current_pane():
    cmds = _inside_tmux_commands("/proj", "hunch run", "%5")
    flat = [tok for c in cmds for tok in c]
    # never spawns a new session / window / claude — the live session stays put
    assert "new-session" not in flat
    assert "new-window" not in flat
    assert "claude" not in flat
    assert cmds[0][-1] == "hunch panel"
    assert cmds[1][-1] == "hunch run"
    # split relative to the current pane; focus returns to it
    assert "%5" in cmds[0]
    assert cmds[-1] == ["tmux", "select-pane", "-t", "%5"]


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
