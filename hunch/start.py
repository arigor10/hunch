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
        ["tmux", "set-option", "-p", "-t", f"{w}.0", "@hunch_role", "research"],
        ["tmux", "set-option", "-p", "-t", f"{w}.1", "@hunch_role", "panel"],
        ["tmux", "set-option", "-p", "-t", f"{w}.2", "@hunch_role", "run"],
        ["tmux", "select-pane", "-t", f"{w}.0"],
    ]


def _parse_roles(list_panes_output: str) -> dict[str, str]:
    """Map `@hunch_role` -> pane_id from `tmux list-panes -F "#{pane_id} #{@hunch_role}"`.
    Untagged panes (empty role) are skipped. Lets a re-run see what's already present."""
    roles: dict[str, str] = {}
    for line in list_panes_output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            roles[parts[1]] = parts[0]
    return roles


def _window_roles() -> dict[str, str]:
    out = subprocess.run(
        ["tmux", "list-panes", "-F", "#{pane_id} #{@hunch_role}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return _parse_roles(out)


def _tag_pane(pane_id: str, role: str) -> None:
    subprocess.run(
        ["tmux", "set-option", "-p", "-t", pane_id, "@hunch_role", role], check=True
    )


_SHELLS = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}


def _pane_command(pane_id: str) -> str:
    """The pane's foreground command (e.g. 'node' while Claude runs, 'bash' when idle)."""
    return subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_command}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _research_is_idle(research_id: str | None, pane_command: str | None) -> bool:
    """True when there's no live research agent — either no research pane, or the
    tagged pane has fallen back to an idle shell (Claude was stopped). The research
    pane is the user's persistent shell, so role-existence alone can't tell us."""
    if research_id is None:
        return True
    return pane_command in _SHELLS


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
    """Add only the missing Hunch panes beside the current pane. Idempotent: panes
    we created carry an `@hunch_role` tag, so a re-run fills in only what's gone
    (a closed run pane, a not-yet-started claude) and never duplicates."""
    cur = subprocess.run(
        ["tmux", "display-message", "-p", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    roles = _window_roles()
    research_pane = roles.get("research", cur)

    # Panel (top-right): create beside the research pane only if absent.
    if "panel" not in roles:
        panel = subprocess.run(
            ["tmux", "split-window", "-h", "-l", RIGHT_COLUMN, "-t", research_pane,
             "-c", cwd, "-P", "-F", "#{pane_id}", "hunch panel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _tag_pane(panel, "panel")
        roles["panel"] = panel

    # Run (below the panel): create only if absent.
    if "run" not in roles:
        run_pane = subprocess.run(
            ["tmux", "split-window", "-v", "-t", roles["panel"],
             "-c", cwd, "-P", "-F", "#{pane_id}", run_cmd],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _tag_pane(run_pane, "run")

    # Research (claude): (re)launch when there's no research pane, or the tagged one
    # has fallen back to an idle shell (you stopped Claude).
    research_id = roles.get("research")
    pane_cmd = _pane_command(research_id) if research_id else None
    if _research_is_idle(research_id, pane_cmd):
        target = research_id or cur
        _tag_pane(target, "research")
        research_pane = target
        # Interactive caller → (re)launch the resumed agent. The onboarding agent
        # (non-TTY) only ever lands here on first run, when Claude is already there.
        if sys.stdout.isatty() and shutil.which("claude"):
            if target == cur:
                subprocess.run(["tmux", "select-pane", "-t", cur], check=True)
                os.chdir(cwd)
                os.execvp("bash", ["bash", "-c", RESEARCH_CMD])  # replaces this process
            else:
                subprocess.run(
                    ["tmux", "send-keys", "-t", target, RESEARCH_CMD, "Enter"], check=True
                )
    else:
        research_pane = research_id

    subprocess.run(["tmux", "select-pane", "-t", research_pane], check=True)
    sys.stdout.write("hunch start: workspace ready (added only what was missing).\n")
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
