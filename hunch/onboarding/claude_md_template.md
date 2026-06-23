# {{PROJECT_TITLE}}

## What This Project Is

{{ONE_OR_TWO_SENTENCES: the research question or goal.}}

## Direction

The goal, success criteria, and scope live in [`vision.md`](vision.md).
{{ONE_LINE_SUMMARY of the direction, so the goal is visible at a glance.}}

## Compute & Budget

{{Local GPUs? API-only? Cost ceiling? Omit this section entirely if not applicable.}}

## Autonomy

Defaults per the research conventions: proceed on implementation and pre-planned
experiments; check in before changing direction, benchmark strategy, ambiguous-result
interpretation, or method architecture. {{Any project-specific adjustment.}}

## Project structure & path bindings

Where the research conventions' slots live in this project:

- Plan docs & experiment summaries → {{e.g. docs/experiments/}}
- Results registry → {{e.g. docs/results_registry.md}}
- Decisions log → {{e.g. docs/decisions_log.md, or numbered ADRs under decisions/}}
- Literature notes → {{e.g. literature/}}

{{If this is an existing codebase, briefly map its layout here — or keep the descriptive
map already generated for the repo.}}

## Key documents

- [`vision.md`](vision.md) — goal, success criteria, scope.
- {{others as they appear: plan, results registry, decisions log.}}

---

@research_conventions.md
