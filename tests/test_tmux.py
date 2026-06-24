"""Tests for hunch/tmux.py — pure parsing + the relay send (mocked subprocess)."""

from __future__ import annotations

import subprocess

import pytest

from hunch import tmux


def test_parse_roles_maps_role_to_pane_and_skips_untagged():
    out = "%1 research\n%2 panel\n%3 \n%4 run\n"
    assert tmux.parse_roles(out) == {"research": "%1", "panel": "%2", "run": "%4"}


def test_window_roles_empty_when_not_in_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert tmux.window_roles() == {}


def test_current_pane_id_none_when_not_in_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert tmux.current_pane_id() is None


def test_send_text_to_pane_runs_load_paste_enter_in_order(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(tmux.subprocess, "run", fake_run)
    tmux.send_text_to_pane("%1", "hello\nworld")

    assert calls[0][:2] == ["tmux", "load-buffer"]
    assert calls[1][:2] == ["tmux", "paste-buffer"]
    assert calls[2][:2] == ["tmux", "send-keys"]
    assert calls[2][-1] == "Enter"
    assert "%1" in calls[1] and "%1" in calls[2]


def test_send_text_to_pane_raises_relay_error_on_failure(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(tmux.subprocess, "run", boom)
    with pytest.raises(tmux.RelayError):
        tmux.send_text_to_pane("%1", "hello")
