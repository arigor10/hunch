"""Thin helpers for talking to tmux.

Shared by `hunch start` (which builds the working layout) and the panel relay
(which types an approved hunch into the research pane). Kept dependency-free so
both can import it cheaply. Every query degrades to a safe default when tmux is
absent or errors, so callers don't have to wrap them in try/except.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def in_tmux() -> bool:
    """True if we're running inside a tmux session."""
    return bool(os.environ.get("TMUX"))


def parse_roles(list_panes_output: str) -> dict[str, str]:
    """Map ``@hunch_role`` -> pane_id from
    ``tmux list-panes -F "#{pane_id} #{@hunch_role}"``.

    Untagged panes (empty role) are skipped, so a caller sees only the panes
    Hunch itself tagged.
    """
    roles: dict[str, str] = {}
    for line in list_panes_output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            roles[parts[1]] = parts[0]
    return roles


def window_roles() -> dict[str, str]:
    """``@hunch_role`` -> pane_id for the current tmux window.

    Returns ``{}`` when not in tmux, tmux is unavailable, or the command errors.
    """
    if not in_tmux() or not tmux_available():
        return {}
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-F", "#{pane_id} #{@hunch_role}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    return parse_roles(out)


def current_pane_id() -> str | None:
    """The pane id we're running in, or ``None`` if unavailable."""
    if not in_tmux() or not tmux_available():
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return out or None


class RelayError(RuntimeError):
    """A tmux relay step failed — the keystrokes did not reach the pane."""


def send_text_to_pane(
    pane_id: str, text: str, *, buffer_name: str = "hunch_relay"
) -> None:
    """Type ``text`` into ``pane_id`` and submit it (as if pasted, then Enter).

    Uses ``load-buffer``/``paste-buffer`` rather than ``send-keys <text>`` so
    multi-line content and shell-special characters can't be mangled or
    partially interpreted. Raises :class:`RelayError` if any step fails — the
    caller treats that as "not delivered" and does not record the hunch as
    surfaced.
    """
    try:
        subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            input=text.encode(), check=True, capture_output=True,
        )
        # -d: delete the buffer after pasting so it doesn't linger.
        subprocess.run(
            ["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", pane_id],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "Enter"],
            check=True, capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RelayError(str(exc)) from exc
