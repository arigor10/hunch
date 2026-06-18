"""`hunch onboard` — prepare a project for the agent-guided research setup.

Materializes the onboarding kit assets into the project so the onboarding
agent (a `claude` session following `onboarding.md`) finds them at known
paths — then the CLI prints the kickoff instruction. The actual setup
(interview, CLAUDE.md, vision, hooks via `hunch init`) is done by the agent
per the procedure; this command only lays down the materials.

Layout:
  - research_conventions.md            -> project ROOT (permanent; CLAUDE.md
                                          @-includes it, so it lives in the repo)
  - .hunch/onboarding/onboarding.md         -> the procedure the agent follows
  - .hunch/onboarding/claude_md_template.md -> the template the agent fills

The procedure + template are transient setup material and live under `.hunch/`,
which `hunch init` gitignores. `research_conventions.md` is intentionally at the
root (it is part of the committed project setup, not a local artifact).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hunch.onboarding import (
    CLAUDE_MD_TEMPLATE,
    ONBOARDING,
    RESEARCH_CONVENTIONS,
    read_asset,
)


@dataclass(frozen=True)
class OnboardResult:
    """What `hunch onboard` materialized."""
    cwd: Path
    conventions_path: Path
    conventions_existed: bool
    procedure_path: Path
    template_path: Path

    def as_lines(self) -> list[str]:
        lines = []
        if self.conventions_existed:
            lines.append(f"  existing {self.conventions_path} (left as-is)")
        else:
            lines.append(f"  created  {self.conventions_path}")
        lines.append(f"  created  {self.procedure_path}")
        lines.append(f"  created  {self.template_path}")
        return lines

    def kickoff_lines(self) -> list[str]:
        rel = self.procedure_path.relative_to(self.cwd)
        return [
            "",
            "Next: run `claude` in this directory and tell it:",
            f'  "Follow {rel} to set up this project for agentic research."',
        ]


def onboard_project(cwd: Path) -> OnboardResult:
    """Materialize the onboarding kit assets into the project at `cwd`.

    Idempotent. The transient procedure/template under `.hunch/onboarding/` are
    always refreshed to the installed kit version; an existing root
    `research_conventions.md` is left untouched (it is the user's file once
    placed and may carry local edits).
    """
    cwd = Path(cwd)
    staging = cwd / ".hunch" / "onboarding"
    staging.mkdir(parents=True, exist_ok=True)

    procedure_path = staging / ONBOARDING
    procedure_path.write_text(read_asset(ONBOARDING))

    template_path = staging / CLAUDE_MD_TEMPLATE
    template_path.write_text(read_asset(CLAUDE_MD_TEMPLATE))

    conventions_path = cwd / RESEARCH_CONVENTIONS
    conventions_existed = conventions_path.exists()
    if not conventions_existed:
        conventions_path.write_text(read_asset(RESEARCH_CONVENTIONS))

    return OnboardResult(
        cwd=cwd,
        conventions_path=conventions_path,
        conventions_existed=conventions_existed,
        procedure_path=procedure_path,
        template_path=template_path,
    )
