"""`hunch start` — open the recommended working layout in tmux.

Layout: the research agent (`claude`) on the LEFT (~65% — most monitors are wider
than tall), with Hunch's two windows stacked on the RIGHT: `hunch panel` (top) and
`hunch run` (bottom).

Behaviour by context (never forces tmux):
  - not set up (`.hunch/replay` missing) → tell the user to onboard/init first.
  - tmux not installed                   → print the manual layout, exit 0.
  - already inside tmux (`$TMUX`)        → add the two Hunch panes *beside the current
                                           pane*, keeping whatever's running there
                                           (e.g. your live research session) untouched.
  - otherwise                            → create a fresh detached session (claude +
                                           panel + run) and attach.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SESSION = "hunch"
RIGHT_COLUMN = "35%"  # sized with `-l`; research (left) keeps ~65%
# Resume the project's most recent conversation; fall back to a fresh session if
# there is none yet (so it never errors on a never-started project).
RESEARCH_CMD = "claude --continue || claude"


def _run_command(config: str | None) -> str:
    return f"hunch run --config {config}" if config else "hunch run"


def _new_session_commands(cwd: str, run_cmd: str) -> list[list[str]]:
    """Build a fresh detached `hunch` session: claude left, panel top-right, run
    bottom-right. Each pane's process is launched by the split itself (no
    send-keys timing race)."""
    w = f"{SESSION}:0"
    return [
        ["tmux", "new-session", "-d", "-s", SESSION, "-c", cwd, RESEARCH_CMD],
        ["tmux", "split-window", "-h", "-l", RIGHT_COLUMN, "-t", f"{w}.0", "-c", cwd, "hunch panel"],
        ["tmux", "split-window", "-v", "-t", f"{w}.1", "-c", cwd, run_cmd],
        ["tmux", "select-pane", "-t", f"{w}.0"],
    ]


def _inside_tmux_commands(cwd: str, run_cmd: str, cur_pane: str) -> list[list[str]]:
    """Add the two Hunch panes beside the current pane — no new session, window, or
    claude, so the running research session stays put."""
    return [
        ["tmux", "split-window", "-h", "-l", RIGHT_COLUMN, "-t", cur_pane, "-c", cwd, "hunch panel"],
        ["tmux", "split-window", "-v", "-c", cwd, run_cmd],  # splits the active (panel) pane
        ["tmux", "select-pane", "-t", cur_pane],
    ]


def _manual_instructions(run_cmd: str) -> str:
    return (
        "tmux not found — set up the layout by hand (research left; the two Hunch\n"
        "windows stacked on the right):\n"
        "  Pane 1 (left):         claude\n"
        "  Pane 2 (top-right):    hunch panel\n"
        f"  Pane 3 (bottom-right): {run_cmd}\n"
        "Or install tmux and re-run `hunch start`."
    )


def start(cwd: Path, config: str | None = None, attach: bool = True) -> int:
    cwd = Path(cwd)
    if not (cwd / ".hunch" / "replay").is_dir():
        sys.stderr.write(
            f"hunch start: {cwd} is not set up for Hunch (no .hunch/replay). "
            f"Run `hunch onboard` or `hunch init` first.\n"
        )
        return 1

    run_cmd = _run_command(config)
    if not shutil.which("tmux"):
        sys.stdout.write(_manual_instructions(run_cmd) + "\n")
        return 0

    cwd_s = str(cwd)
    if os.environ.get("TMUX"):
        return _start_inside_tmux(cwd_s, run_cmd)
    return _start_new_session(cwd_s, run_cmd, attach)


def _start_inside_tmux(cwd: str, run_cmd: str) -> int:
    cur = subprocess.run(
        ["tmux", "display-message", "-p", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    for cmd in _inside_tmux_commands(cwd, run_cmd, cur):
        subprocess.run(cmd, check=True)
    # If a person is driving this interactively, turn their current pane into the
    # (resumed) research agent. If a non-TTY caller ran it — e.g. the onboarding
    # agent from its own bash — leave the pane alone; Claude is already there.
    if sys.stdout.isatty() and shutil.which("claude"):
        os.chdir(cwd)
        os.execvp("bash", ["bash", "-c", RESEARCH_CMD])  # replaces this process
    sys.stdout.write(
        "hunch start: added `hunch panel` + `hunch run` beside your current pane.\n"
    )
    return 0


def _start_new_session(cwd: str, run_cmd: str, attach: bool) -> int:
    exists = subprocess.run(
        ["tmux", "has-session", "-t", SESSION], capture_output=True
    ).returncode == 0
    if not exists:
        for cmd in _new_session_commands(cwd, run_cmd):
            subprocess.run(cmd, check=True)
    if attach:
        os.execvp("tmux", ["tmux", "attach", "-t", SESSION])  # hand off the terminal
    sys.stdout.write(
        f"hunch start: session '{SESSION}' ready — attach with `tmux attach -t {SESSION}`\n"
    )
    return 0
