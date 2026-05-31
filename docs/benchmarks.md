# Benchmarks

**Status:** living document, last updated 2026-05-11

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

## Critic recall against mined ground truth

**Question:** What fraction of real scientific concerns does the critic actually catch?

### Ground truth construction

We retrospectively mined ground-truth concerns from one project's full conversation history (~6,100 replay events across 79 sessions). The mining pipeline (see [mining_pipeline.md](mining_pipeline.md)) works in two stages:

1. **Nose mining:** An LLM reads the conversation in overlapping segments and identifies moments where the Scientist explicitly flagged an anomaly — a contradictory result, a forgotten commitment, a questionable interpretation. This produced 108 nose moments.
2. **Evidence mining:** For each nose moment, an agent searches backward through the full conversation to find the *earliest point* where enough evidence existed to catch the concern — before the Scientist noticed it. This produces a hunch with `evidence_tick_seqs` = the full evidence trail.

The 108 mined hunches were ingested into the bank via `hunch bank sync`, which dedup-matched them against existing critic output. After dedup, they map to **102 unique bank concepts** (some mined hunches collapsed to the same concern). All are labeled tp (true positive by construction — the Scientist actually noticed each concern).

### Results

Four critic runs across two architectures and three models were evaluated, all
over the **full** dataset. Two are model-matched accumulators (Sonnet 4.5 vs
Opus 4.6 — same prompt, watermarks, and trigger; only the model differs), which
isolates the effect of the underlying model. (`wiki-v1-ar-004` was resumed from
a ~50% checkpoint and completed 2026-05-30; the Opus accumulator
`accum-opus46-ar-001` ran 2026-05-31 on `claude-opus-4-6` — the model that
actually conducted this AR research.) Caught/recall are computed from bank
links: a ground-truth concern is "caught" when one of a run's hunches links to
(or sources) the same bank entry as a mined hunch. No LLM scoring — the links
already exist from `hunch bank sync`.

| Run | Architecture | Model | Survivors (post-filter) | Caught | Recall |
|---|---|---|---|---|---|
| Sonnet accumulator (`ar_v1.1_multi`) | accumulator v0.1 | Sonnet 4.5 | 126 | 20 | **19.6%** |
| **Opus accumulator (`accum-opus46-ar-001`)** | accumulator v0.1 | **Opus 4.6** | 77 | 12 | **11.8%** |
| DeepSeek accumulator (`accum-deepseek-v4-pro-001`) | accumulator v0.1 | DeepSeek V4 Pro | 175 | 13 | **12.7%** |
| Sonnet wiki (`wiki-v1-ar-004`) | wiki v1 | Sonnet 4.5 | 142 | 13 | **12.7%** |
| **Sonnet combined** | both | Sonnet 4.5 | 268 | 27 | **26.5%** |
| **All four combined** | — | — | 520 | 38 | **37.3%** |

The **accumulator v0.1** is a sliding-window critic that receives a compacted
summary accumulated across the full session. Each tick appends new conversation
to the summary and asks the model to identify anomalies. The **wiki v1** is an
agentic critic that maintains a persistent structured knowledge base (wiki)
across ticks, reading and writing entity files with tool use.

64 of 102 ground-truth concepts (63%) were missed by every critic.

**Model dependence (Sonnet 4.5 vs Opus 4.6, same accumulator):** the headline,
and a surprising one — **the stronger/more-expensive model recalled *fewer*
ground-truth concerns: Opus 11.8% vs Sonnet 19.6%.** Recall depends heavily on
the model, but *not* monotonically in model capability. Mechanism (from the run
data): Opus emitted ~the same *raw* hunch volume (226 vs Sonnet's 247) but those
collapsed to far fewer **distinct** concerns after dedup (77 survivors vs 126;
66% of Opus's raw hunches were near-duplicates vs ~49% for Sonnet). Opus is
*verbose-repetitive* — it re-flags the same handful of concerns across many
ticks rather than spreading across new ones, so it covers less ground truth.

Three caveats temper any "Opus is a worse critic" reading:
1. **Self-authorship.** Opus 4.6 conducted this AR research. We expected
   familiarity might *inflate* its recall; it scored *lower* instead — so either
   familiarity didn't help or it actively suppressed flags ("I remember this
   resolved fine"). A clean control needs Opus critiquing a project it did not
   author.
2. **Prompt fit.** The accumulator prompt was iterated on Sonnet; Opus's
   verbose-repetitive behavior may be partly a prompt-fit artifact, not pure
   capability. A prompt-iteration pass on Opus is warranted before ranking.
3. **n=1 project, 102 concerns.** Small sample.

So the result is illuminating but not yet a clean capability ranking: it
establishes that model choice moves recall by ~8 points on identical scaffolding
and in an unexpected direction.

**Architecture (both Sonnet 4.5):** the wiki (12.7%) also trails the accumulator
(19.6%) on overall recall — consistent with the SP finding that the
sliding-window accumulator out-recalls the wiki on mined ground truth at equal
model. But see the overlap analysis: on AR the wiki contributes a substantial
set of *exclusive* catches the accumulators miss, which it did **not** on SP.

### Overlap between critics

Among the four full runs, of the 102 ground-truth concerns:

| | Caught | Exclusive (only this run) | Shared with ≥1 other |
|---|---|---|---|
| Sonnet accumulator | 20 | 8 | 12 |
| Opus accumulator | 12 | 6 | 6 |
| DeepSeek accumulator | 13 | 5 | 8 |
| Sonnet wiki | 13 | 5 | 8 |

Pairwise shared catches: wiki∩Sonnet-accum = 6, wiki∩DeepSeek-accum = 2,
Sonnet-accum∩DeepSeek-accum = 7; Opus-accum∩Sonnet-accum = 5,
Opus-accum∩DeepSeek-accum = 4, Opus-accum∩wiki = 3.

**The Opus accumulator is not a subset of the Sonnet one.** Of its 12 catches, 6
are exclusive among all four runs — so the two models surface partly *different*
concerns. Model choice changes not just *how much* you catch but *what* you
catch, which is why "All four combined" reaches 37.3% vs 31.4% for the three
Sonnet/DeepSeek runs (Opus adds 6 net-new).

**The wiki adds 5 exclusive catches** (among all four runs) — concerns no other
critic caught — despite its lower total recall. This is the key difference from
SP, where the wiki had **0** exclusive catches. The wiki's unique AR catches
lean toward longer-range and process concerns that need cross-tick state to
surface:

- `hb-0142`: BiPO L16 Pareto relationship reversed between original and rerun
- `hb-0182`: mechanistic "linear regime advantage" stated as fact before tested
- `hb-0234`: DC-only KL (0.004) is below the exp-023 noise floor (0.006) — the "signal" is undetectable
- `hb-0262`: asymmetry regularizer Pareto-improves BiPO despite DPO's implicit KL already blocking leakage
- `hb-0263`: telescopic "fails" verdict never re-examined after a retroactive confound audit

(A sixth — `hb-0155`, Step 1 L16 drift violating the plan's pre-registered
failure criterion — was exclusive to the wiki among the three Sonnet/DeepSeek
runs but is now shared with the Opus accumulator.)

So while the wiki under-recalls the accumulator head-to-head, it is genuinely
**complementary** on AR: combining the wiki with the Sonnet accumulator lifts
recall from 19.6% → 26.5%, and all four together reach 37.3%. The earlier SP
conclusion ("the wiki caught nothing the accumulators missed") does **not**
generalize — on a longer, more experiment-dense project the persistent wiki
surfaces distinct concerns.

*(Caveat: numbers are recomputed from the live bank as of 2026-05-31 and differ
slightly from earlier snapshots — e.g. the DeepSeek accumulator is 13 here vs 14
in a prior snapshot — because intervening re-syncs adjusted a few links. All
rows in this table are from the same bank state, so they are mutually
consistent.)*

### Evidence accessibility analysis

Not all ground-truth concerns are structurally reachable by the critic at runtime. We analyzed two accessibility barriers:

**Compaction boundaries.** Each session starts with a compacted summary of prior context. Evidence that predates the current session is only available through this lossy summary.

| Evidence span | Findings | % of 108 |
|---|---|---|
| Within a single session | 8 | 7.4% |
| Crosses session boundaries | 100 | 92.6% |

| Sessions spanned | Findings |
|---|---|
| 1 | 8 |
| 2 | 30 |
| 3 | 34 |
| 4 | 20 |
| 5+ | 16 |

92.6% of ground-truth concerns require connecting evidence across multiple sessions — information that the original LLM session had already compacted away. The median concern spans 3 sessions.

**Accumulator window.** The accumulator maintains a rolling context window of ~140K tokens. Evidence that has been purged from this window is inaccessible regardless of compaction.

| | Findings | % of 108 |
|---|---|---|
| All evidence fits in accumulator window | 80 | 74.1% |
| Evidence falls outside window | 28 | 25.9% |

74% of concerns have all evidence within the accumulator's window at signal time — the accumulator *could* see them. Its miss rate on these (~64%) reflects prompt and model limitations, not structural ones. The remaining 26% are structurally unreachable: the earliest evidence was already purged by the time the signal arrived.

**Wiki-exclusive catches.** The 3 concerns caught only by the wiki (not by either accumulator) all had evidence that fit within the accumulator window. These are prompt/architecture wins — the wiki's persistent knowledge base and agentic reasoning found patterns the accumulator's single-pass prompt missed with the same information available.

### Observations

- **The recall ceiling is ~28%.** No combination of critics catches more than 29/102 concepts. The 73 missed concerns span the full project timeline and include deep architectural reasoning, mathematical mechanism gaps, and cross-experiment contradictions.
- **Cross-session evidence is the norm.** 93% of ground-truth concerns require evidence from multiple sessions, making this the structural case for accumulated context: a critic with no memory across sessions would miss nearly everything.
- **The gap is mostly capability, not structure.** 74% of concerns are within the accumulator's window, yet only ~25% of those are caught. Improving the prompt, model, or reasoning approach has more headroom than expanding the context window.
- **Model diversity helps.** Sonnet and DeepSeek catch partially different subsets (only 8 shared catches out of 29 total). Ensembling across models improves coverage.
