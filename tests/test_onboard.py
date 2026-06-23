"""Tests for `hunch onboard` — materialize the onboarding kit into a project."""

from __future__ import annotations

from hunch.cli import main as cli_main
from hunch.onboarding import (
    CLAUDE_MD_TEMPLATE,
    ONBOARDING,
    RESEARCH_CONVENTIONS,
    read_asset,
)
from hunch.onboarding.onboard import onboard_project


def test_onboard_materializes_assets(tmp_path):
    result = onboard_project(tmp_path)

    # research_conventions.md at the root, with the real kit content.
    conv = tmp_path / RESEARCH_CONVENTIONS
    assert conv.is_file()
    assert conv.read_text() == read_asset(RESEARCH_CONVENTIONS)
    assert result.conventions_existed is False

    # procedure + template staged under .hunch/onboarding/.
    proc = tmp_path / ".hunch" / "onboarding" / ONBOARDING
    tmpl = tmp_path / ".hunch" / "onboarding" / CLAUDE_MD_TEMPLATE
    assert proc.read_text() == read_asset(ONBOARDING)
    assert tmpl.read_text() == read_asset(CLAUDE_MD_TEMPLATE)


def test_onboard_does_not_clobber_existing_conventions(tmp_path):
    conv = tmp_path / RESEARCH_CONVENTIONS
    conv.write_text("MY EDITED CONVENTIONS\n")

    result = onboard_project(tmp_path)

    assert result.conventions_existed is True
    assert conv.read_text() == "MY EDITED CONVENTIONS\n"  # preserved, not clobbered


def test_onboard_idempotent_refreshes_staged_assets(tmp_path):
    onboard_project(tmp_path)
    onboard_project(tmp_path)  # second run must not error
    proc = tmp_path / ".hunch" / "onboarding" / ONBOARDING
    assert proc.read_text() == read_asset(ONBOARDING)


def test_cli_onboard_prints_kickoff(tmp_path, capsys):
    rc = cli_main(["onboard", "--cwd", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "research_conventions.md" in out
    assert ".hunch/onboarding/onboarding.md" in out
    assert "Follow" in out          # the kickoff instruction
    assert "claude" in out


def test_cli_onboard_missing_directory(tmp_path, capsys):
    rc = cli_main(["onboard", "--cwd", str(tmp_path / "nope")])
    assert rc == 1
    assert "not a directory" in capsys.readouterr().err
