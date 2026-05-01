# Benchmarks

**Status:** living document, last updated 2026-04-30

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

## Meeting-room vs raw transcript

**Question:** Does the meeting-room abstraction (compacted dialogue summaries) help the critic catch things that a raw-transcript critic misses?

**Setup:** The `soft_prompting` project's Claude Code session (18K events, 39 compaction boundaries, 40 context windows) was processed two ways:

1. **Meeting-room critic:** DeepSeek V4 Pro via the standard Hunch pipeline — sees a compacted meeting-room summary that accumulates across the full session. 240 ticks, 103 hunches emitted (after dedup+novelty filtering).
2. **Raw-transcript critic:** DeepSeek V4 Pro given the full-resolution raw transcript, one chunk per compaction window. Each chunk is independent — no accumulated context. 40 chunks, 60 hunches emitted.

The raw hunches were deduped (sliding window of 10), novelty-filtered against their source chunks, then cross-matched against all 103 meeting-room hunches using the dedup judge.

### Raw-transcript pipeline

| Stage | Count |
|---|---|
| Raw hunches emitted | 60 |
| After intra-run dedup | 52 (-8 dups) |
| After novelty filter | 39 (-13 already raised) |

### Cross-match results

| Metric | Value |
|---|---|
| Raw-only (not in meeting-room) | 21 |
| Meeting-room-only (not in raw) | 88 |
| Cross-matches (overlap) | 18 |
| Raw recall of meeting-room | 18/103 = 17.5% |

The raw transcript critic recovers only 17.5% of the meeting-room critic's catches. The meeting-room catches 5x more unique concerns. Raw-only hunches tend to be local/operational (CUDA warnings, memory discrepancies, miscounts within a single chunk), while meeting-room-only hunches are long-range scientific tensions across experiments.

### Cross-compaction citation analysis

To quantify how far back the meeting-room critic reaches, we mapped each of the 103 meeting-room hunches' cited evidence (`triggering_refs.chunks`) back to the original Claude Code session's 40 compaction windows. A hunch "spans N compaction boundaries" if its earliest cited chunk is from a compaction window N windows before the trigger window.

| Hunches citing across... | Count | % of 103 |
|---|---|---|
| >= 1 compaction boundary | 51 | 50% |
| >= 2 compaction boundaries | 23 | 22% |
| >= 5 compaction boundaries | 2 | 2% |

**50% of the meeting-room critic's hunches connect evidence across at least one Claude Code compaction boundary** — information that was compacted away from the original session and is only accessible through the accumulated meeting-room summary.

Additionally, 72/103 (70%) of hunches cite evidence from before their trigger window (even if within the same compaction window), meaning the accumulated timeline — not just the fresh chunks — is the primary evidence source.

### Gap distribution (replay-buffer chunks before trigger window)

```
gap= 0:  52 hunches (local only)
gap= 1:  28
gap= 2:  14
gap= 3:   2
gap= 4:   5
gap= 6:   1
gap= 7:   1
```

The largest gap is 7 compaction boundaries: h-0167 ("exp_005b 'H4: Decisively refuted' contradicts exp_009 warm-start finding") connects evidence from compaction window 27 to window 34.

### Caveats

- Single project (`soft_prompting`), single model (DeepSeek V4 Pro). The meeting-room advantage may vary by project length and model.
- Cross-match uses the dedup judge prompt, which was designed for same-run duplicates, not cross-run semantic matching. Some true overlaps may be missed.
- The raw-transcript critic sees each chunk independently (no memory across chunks). A raw critic with sliding-window context would likely perform better, but would still lack the full-session accumulated view.
- Compaction boundary mapping uses timestamps to correlate replay events to the original session. All 3,459 replay events matched (100%).
