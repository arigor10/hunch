# Benchmarks

**Status:** living document, last updated 2026-04-29

Early benchmark results for Hunch components. These are not paper-ready — the datasets are small and drawn from a single project — but they establish baselines and guide iteration. Results will be updated as more labeled data accumulates.

## Dedup filter accuracy

**Task:** Given two hunch descriptions, decide whether they flag the same underlying concern (duplicate) or distinct concerns (not duplicate).

**Dataset:** 31 hunch pairs (11 duplicate, 20 non-duplicate) from the `soft_prompting` project, human-labeled by the project Scientist. Source: `agentic_research_critic/data/dedup_eval_pairs.jsonl`.

**Method:** Each pair is evaluated independently by the model using the dedup judge prompt. Predictions are compared against human labels. See `agentic_research_critic/scripts/eval_dedup.py` for the harness.

### Results by model and prompt version

Production uses `judge_dedup.md` (baseline prompt) with Haiku 4.5.

| Model | Prompt | Precision | Recall | F1 | Accuracy | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|---|
| **Haiku 4.5** | **baseline** | **91.7%** | **100%** | **0.957** | **96.8%** | 11 | 1 | 0 | 19 |
| Haiku 4.5 | v2 | 83.3% | 90.9% | 0.870 | 90.3% | 10 | 2 | 1 | 18 |
| Haiku 4.5 | v3 | 90.9% | 90.9% | 0.909 | 93.5% | 10 | 1 | 1 | 19 |
| Sonnet 4.5 | baseline | 84.6% | 100% | 0.917 | 93.5% | 11 | 2 | 0 | 18 |
| Sonnet 4.5 | v3 | 90.9% | 90.9% | 0.909 | 93.5% | 10 | 1 | 1 | 19 |
| Gemma 3 27B | baseline | 55.0% | 100% | 0.710 | 71.0% | 11 | 9 | 0 | 11 |
| Gemma 3 27B | v2 | 47.8% | 100% | 0.647 | 61.3% | 11 | 12 | 0 | 8 |
| Gemma 3 27B | v3 | 64.7% | 100% | 0.786 | 80.6% | 11 | 6 | 0 | 14 |
| Gemini 3.1 Pro | baseline | 100% | 9.1% | 0.167 | 67.7% | 1 | 0 | 10 | 20 |
| Gemini 3.1 Pro | baseline (512tok) | 90.9% | 90.9% | 0.909 | 93.5% | 10 | 1 | 1 | 19 |
| Gemini 3.1 Pro | v3 | 0% | 0% | 0.000 | 64.5% | 0 | 0 | 11 | 20 |
| Gemini 3.1 Pro | v3 (512tok) | 100% | 18.2% | 0.308 | 71.0% | 2 | 0 | 9 | 20 |

### Observations

- **Haiku baseline is the best overall:** F1=0.957 with zero missed duplicates and only one false positive. Surprisingly, the smaller and cheaper model outperforms Sonnet on this task.
- **Prompt iteration didn't help:** v2 and v3 prompts didn't improve over the baseline for Anthropic models. The baseline prompt is kept in production.
- **Gemma has a precision problem:** perfect recall but many false positives — it over-matches. The v3 prompt helps somewhat (FP drops from 9→6) but the gap remains large.
- **Gemini is highly sensitive to max_tokens:** with default max_tokens, Gemini barely predicts any duplicates (recall ~9%). Constraining to 512 tokens fixes this, suggesting the model was hedging in longer responses. This is a model-specific quirk, not a prompt issue.

### Caveats

- **n=31** is small. Confidence intervals are wide (e.g., Haiku's precision 95% CI is roughly 73%–99% by Wilson interval). These numbers guide iteration, not publication.
- All pairs are from one project (`soft_prompting`). Cross-project generalization is untested.
- The dedup judge in the bank sync pipeline operates within a bookmark window (±k ticks), so the eval slightly overstates difficulty — some non-duplicate pairs would never be compared in production.
