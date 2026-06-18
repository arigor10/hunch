"""Agentic-research onboarding kit.

Bundled assets for setting up (or retrofitting) a project for agentic research:

- ``research_conventions.md`` — the portable, path-agnostic research-process layer,
  pulled into a project's CLAUDE.md via ``@research_conventions.md``.
- ``onboarding.md`` — the agent-guided onboarding procedure (the orchestrator).
- ``claude_md_template.md`` — the thin project CLAUDE.md skeleton the onboarding fills.

These are *content*, not code — materialized into a user's project by the onboarding
flow. They live in their own package so the kit stays logically separable from the
critic (it can be used with or without Hunch's critic).
"""
from importlib import resources

RESEARCH_CONVENTIONS = "research_conventions.md"
ONBOARDING = "onboarding.md"
CLAUDE_MD_TEMPLATE = "claude_md_template.md"


def read_asset(name: str) -> str:
    """Return the text of a bundled onboarding asset.

    Raises FileNotFoundError if the asset isn't packaged — fail loud rather than
    returning empty content that would silently produce a broken setup.
    """
    resource = resources.files(__package__) / name
    if not resource.is_file():
        raise FileNotFoundError(f"onboarding asset not packaged: {name!r}")
    return resource.read_text(encoding="utf-8")
