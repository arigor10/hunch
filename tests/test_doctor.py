"""Tests for `hunch doctor` — preflight health check."""

from __future__ import annotations

from hunch.cli import main as cli_main
from hunch.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    DoctorReport,
    _check_api_keys,
    _check_claude_auth,
    _check_gitignore,
    _check_hooks,
    _check_replay_dir,
    run_checks,
)
from hunch.init import init_project


def test_claude_auth_is_warn_never_ok():
    """Anti-fake-validation guarantee: auth can't be verified non-interactively,
    so it must never be reported as a green/OK check."""
    c = _check_claude_auth()
    assert c.status == WARN
    assert c.status != OK


def test_api_keys_warn_when_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert _check_api_keys().status == WARN


def test_api_keys_ok_when_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    c = _check_api_keys()
    assert c.status == OK
    assert "OPENROUTER_API_KEY" in c.detail


def test_hooks_fail_on_fresh_then_ok_after_init(tmp_path):
    assert _check_hooks(tmp_path).status == FAIL
    init_project(tmp_path)
    assert _check_hooks(tmp_path).status == OK


def test_replay_fail_on_fresh_then_ok_after_init(tmp_path):
    assert _check_replay_dir(tmp_path).status == FAIL
    init_project(tmp_path)
    assert _check_replay_dir(tmp_path).status == OK


def test_gitignore_skipped_when_not_git_repo(tmp_path):
    # No .git → nothing to isolate → OK (not a warning).
    assert _check_gitignore(tmp_path).status == OK


def test_gitignore_warn_in_git_repo_then_ok_after_init(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _check_gitignore(tmp_path).status == WARN
    init_project(tmp_path)
    assert _check_gitignore(tmp_path).status == OK


def test_report_ok_property():
    assert DoctorReport([Check("a", OK, ""), Check("b", WARN, "")]).ok is True
    assert DoctorReport([Check("a", OK, ""), Check("b", FAIL, "")]).ok is False


def test_run_checks_includes_all_expected(tmp_path):
    names = {c.name for c in run_checks(tmp_path).checks}
    assert {"hooks wired", "replay buffer", "claude authenticated"} <= names


def test_cli_doctor_fails_on_uninitialized_project(tmp_path, capsys):
    # Fresh dir: hooks + replay FAIL regardless of environment → exit 1.
    rc = cli_main(["doctor", "--cwd", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "hooks wired" in out
    assert "replay buffer" in out
