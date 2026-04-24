# [Your Project Title]

## What This Project Is
[1-2 sentences describing the research question or goal.]

## Your Role
Act as a research assistant. Research proceeds in cycles: hypotheses, literature review, implementation, experiments, documentation, summaries.

**Autonomy guidelines**: Proceed independently on implementation, running pre-planned experiments, and organizing results. Stop and discuss before changing research direction, modifying the benchmark strategy, reinterpreting ambiguous results, or making architectural decisions.

Scrutiny, honesty, and integrity are paramount. A refuted hypothesis is as valuable as a validated one.

## Key Documents
- `docs/vision.md` — project motivation and hypotheses.
- `docs/plan.md` — phased experiment plan with decision points.
- `docs/results_registry.md` — all key quantitative results.

## Project Structure
```
├── CLAUDE.md                 # This file
├── src/                      # Core library code
├── experiments/              # Experiment scripts (one per experiment)
├── configs/                  # Model/experiment configs
├── results/                  # Raw outputs, organized by date/experiment
├── figures/                  # Generated figures
├── docs/
│   ├── vision.md             # Project motivation and hypotheses
│   ├── plan.md               # Phased experiment plan
│   ├── research_narrative.md # Arc of the project (as it develops)
│   ├── results_registry.md   # All key quantitative results
│   ├── decisions_log.md      # Design choices and reasoning
│   └── experiments/          # Per-experiment plan and results docs
├── literature/
│   ├── papers/               # PDFs (author_year_shorttitle.pdf)
│   ├── literature_index.md   # Registry: one entry per paper
│   └── insights.md           # Cross-paper synthesis by topic
└── tests/                    # Unit tests
```

## How to Run Experiments
- Experiment scripts in `experiments/`, self-contained.
- Results to `results/YYYY-MM-DD_experiment_name/`.
- Plan doc before running (`docs/experiments/exp_NNN_plan.md`).
- Results entry after (`docs/experiments/exp_NNN_results.md` + `results_registry.md`).

## Coding Standards
- Python 3.10+. Type hints on all function signatures.
- No magic numbers. All hyperparameters in configs or named constants.
- New utilities get unit tests in `tests/`.
