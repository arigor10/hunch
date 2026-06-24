"""Tests for `hunch doctor` — preflight health check."""

from __future__ import annotations

import json

from hunch.cli import main as cli_main
from hunch.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    DoctorReport,
    _check_api_keys,
    _check_claude_auth,
    _check_claude_cli,
    _check_gitignore,
    _check_hooks,
    _check_no_stale_hooks,
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


# --- edge cases from the cross-family review (Gemini via agy, 2026-06-19) ---

def test_claude_cli_fail_when_missing(monkeypatch):
    monkeypatch.setattr("hunch.doctor.shutil.which", lambda _: None)
    assert _check_claude_cli().status == FAIL


def test_claude_cli_ok_when_version_runs(monkeypatch):
    monkeypatch.setattr("hunch.doctor.shutil.which", lambda _: "/usr/bin/claude")

    class _R:
        returncode = 0
        stdout = "claude 9.9.9"
        stderr = ""

    monkeypatch.setattr("hunch.doctor.subprocess.run", lambda *a, **k: _R())
    c = _check_claude_cli()
    assert c.status == OK
    assert "9.9.9" in c.detail


def test_claude_cli_fail_on_timeout(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr("hunch.doctor.shutil.which", lambda _: "/usr/bin/claude")

    def _boom(*a, **k):
        raise _sp.TimeoutExpired(cmd="claude", timeout=15)

    monkeypatch.setattr("hunch.doctor.subprocess.run", _boom)
    assert _check_claude_cli().status == FAIL


def test_claude_cli_fail_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("hunch.doctor.shutil.which", lambda _: "/usr/bin/claude")

    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr("hunch.doctor.subprocess.run", lambda *a, **k: _R())
    assert _check_claude_cli().status == FAIL


def test_hooks_fail_on_invalid_json(tmp_path):
    s = tmp_path / ".claude" / "settings.local.json"
    s.parent.mkdir(parents=True)
    s.write_text("{ not json")
    assert _check_hooks(tmp_path).status == FAIL


def test_hooks_fail_on_non_dict_hooks(tmp_path):
    s = tmp_path / ".claude" / "settings.local.json"
    s.parent.mkdir(parents=True)
    s.write_text('{"hooks": "not-a-dict"}')
    assert _check_hooks(tmp_path).status == FAIL


def test_hooks_no_crash_on_non_iterable_hook_value(tmp_path):
    # A non-iterable hook value (int) must not crash the check — reported missing → FAIL.
    s = tmp_path / ".claude" / "settings.local.json"
    s.parent.mkdir(parents=True)
    s.write_text('{"hooks": {"UserPromptSubmit": 5, "Stop": 5}}')
    assert _check_hooks(tmp_path).status == FAIL


def test_no_stale_hooks_warns_when_async_delivery_present(tmp_path):
    s = tmp_path / ".claude" / "settings.local.json"
    s.parent.mkdir(parents=True)
    s.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "hunch hook async-delivery",
                    "asyncRewake": True}]},
    ]}}))
    c = _check_no_stale_hooks(tmp_path)
    assert c.status == WARN
    assert "async-delivery" in c.detail


def test_no_stale_hooks_ok_after_init(tmp_path):
    init_project(tmp_path)
    assert _check_no_stale_hooks(tmp_path).status == OK


def test_gitignore_warn_when_unreadable(tmp_path):
    # .gitignore as a directory → read_text raises OSError → graceful WARN, no crash.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").mkdir()
    c = _check_gitignore(tmp_path)
    assert c.status == WARN
    assert "could not read" in c.detail
