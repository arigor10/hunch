"""`hunch doctor` — preflight health check for a Hunch project.

Validates the prerequisites a working setup needs and reports each as
OK / WARN / FAIL. Crucially, it NEVER reports OK for something it could not
actually verify — e.g. it cannot confirm the `claude` CLI is *authenticated*
non-interactively (that would cost a request and risk a hang), so that is a
WARN with guidance, not a green check. A "successful" report that hides an
unverified prerequisite is worse than an honest "couldn't check this."

Exit non-zero iff any check FAILs (hard prerequisites). WARNs do not fail the
command — they flag things the operator must confirm themselves.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hunch.init import (
    STOP_HOOK_COMMAND,
    UPS_HOOK_COMMAND,
    _GITIGNORE_ENTRIES,
    _hook_already_present,
)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_SYMBOL = {OK: "✓", WARN: "⚠", FAIL: "✗"}  # ✓ ⚠ ✗


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        """True iff no hard FAILs (WARNs are allowed)."""
        return all(c.status != FAIL for c in self.checks)

    def as_lines(self) -> list[str]:
        lines = []
        for c in self.checks:
            lines.append(f"  {_SYMBOL.get(c.status, '?')} {c.name}: {c.detail}")
            if c.fix and c.status != OK:
                lines.append(f"      fix: {c.fix}")
        return lines


def run_checks(cwd: Path) -> DoctorReport:
    """Run all health checks for the project rooted at `cwd`."""
    cwd = Path(cwd)
    return DoctorReport(checks=[
        _check_claude_cli(),
        _check_claude_auth(),
        _check_api_keys(),
        _check_hooks(cwd),
        _check_replay_dir(cwd),
        _check_gitignore(cwd),
    ])


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_claude_cli() -> Check:
    path = shutil.which("claude")
    if not path:
        return Check(
            "claude CLI", FAIL, "not found on PATH",
            fix="install Claude Code (https://docs.claude.com/claude-code)",
        )
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return Check("claude CLI", FAIL, f"found at {path} but failed to run ({e})")
    if out.returncode != 0:
        return Check(
            "claude CLI", FAIL,
            f"`claude --version` exited {out.returncode}: {out.stderr.strip()[:120]}",
        )
    return Check("claude CLI", OK, f"{out.stdout.strip()} ({path})")


def _check_claude_auth() -> Check:
    # Auth cannot be verified non-interactively without spending a request and
    # risking a hang. Report honestly rather than fake a green check.
    return Check(
        "claude authenticated", WARN, "cannot verify automatically",
        fix="run `claude` once interactively to confirm you are logged in",
    )


def _check_api_keys() -> Check:
    keys = [k for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY") if os.environ.get(k)]
    if keys:
        return Check("API keys", OK, f"set: {', '.join(keys)}")
    return Check(
        "API keys", WARN, "none set",
        fix="not needed for the default claude_cli backend (uses your "
            "subscription); set OPENROUTER_API_KEY / ANTHROPIC_API_KEY only for "
            "those configs",
    )


def _check_hooks(cwd: Path) -> Check:
    settings = cwd / ".claude" / "settings.local.json"
    if not settings.exists():
        return Check("hooks wired", FAIL, f"{settings} missing", fix="run `hunch init`")
    try:
        data = json.loads(settings.read_text())
    except (OSError, ValueError) as e:
        return Check("hooks wired", FAIL, f"cannot read {settings}: {e}")
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    if not isinstance(hooks, dict):
        return Check("hooks wired", FAIL, "'hooks' is not an object", fix="run `hunch init`")
    missing = [
        name for name, cmd in (
            ("UserPromptSubmit", UPS_HOOK_COMMAND), ("Stop", STOP_HOOK_COMMAND),
        )
        if not _hook_already_present(hooks.get(name, []), cmd)
    ]
    if missing:
        return Check(
            "hooks wired", FAIL, f"missing: {', '.join(missing)}", fix="run `hunch init`",
        )
    return Check("hooks wired", OK, "UserPromptSubmit + Stop present")


def _check_replay_dir(cwd: Path) -> Check:
    replay = cwd / ".hunch" / "replay"
    if replay.is_dir():
        return Check("replay buffer", OK, str(replay))
    return Check("replay buffer", FAIL, f"{replay} missing", fix="run `hunch init`")


def _check_gitignore(cwd: Path) -> Check:
    if not (cwd / ".git").exists():
        return Check("gitignore isolation", OK, "not a git repo (nothing to isolate)")
    gitignore = cwd / ".gitignore"
    text = gitignore.read_text() if gitignore.exists() else ""
    present = {
        line.strip().rstrip("/")
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [e for e in _GITIGNORE_ENTRIES if e.rstrip("/") not in present]
    if missing:
        return Check(
            "gitignore isolation", WARN, f"not ignored: {', '.join(missing)}",
            fix="run `hunch init` to keep Hunch artifacts out of the repo",
        )
    return Check("gitignore isolation", OK, "Hunch artifacts ignored")
