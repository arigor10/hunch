# Hunch — Critic v0 Design

*Companion to `framework_v0.md`. Describes the Critic implementation that ships in v0 — the stub that rides on the framework. The framework is the heavy lift; the Critic is deliberately minimal.*

---

## Purpose & scope

This doc specifies **Critic v0**: the minimal Critic implementation that speaks the Critic protocol defined in `framework_v0.md §3` and emits plausible hunches end-to-end.

**In scope:** Critic implementation substrate, prompt design, output schema, context window, duplicate suppression, the "worth-interrupting" bar.

**Out of scope (deferred):** Agentic/long-running Critic, principle accumulation, scratchpad tree, mentorship dialogue, figure/vision input, cadence tuning.

**Anchor:** Quality is not the bar. *Runnable and non-ridiculous* is the bar. Real Critic quality work comes after the framework is end-to-end.

---

## Architecture

In-process Python wrapping a stateless Sonnet API call. No subprocess, no long-running session, no scratchpad. Each tick:

1. Read last N parsed chunks from `.hunch/replay/conversation.jsonl`.
2. Read current snapshots of all `.md` artifacts from `.hunch/replay/artifacts/`.
3. Read prior hunches (folded state) + their feedback from `hunches.jsonl` + `feedback.jsonl`.
4. Build prompt from template (see § Prompt design).
5. Call Sonnet with prompt caching on the stable prefix.
6. Parse response → 0 or 1 hunch.
7. Return via tick_result.

The module lives at `hunch/critic/stateless_sonnet.py`. It's invoked by the framework as an in-process function in v0 (the Critic-as-process abstraction is *ready* at the protocol layer; swapping to a subprocess or SDK-hosted agent is additive).

---

## Output schema

Each hunch has three fields that matter to downstream consumers:

```jsonc
{
  "hunch_id": "h-0007",                   // assigned by framework
  "smell": "Calibration R² disagrees with today's fit by ~2×",
  "description": "Today's fit in writeups/exp_042.md reports R²=0.94 on held-out. Yesterday's calibration run (chunk c-0031) reported R² in the 0.3–0.5 range on structurally similar data. No methodology change was logged between the two; either the held-out split differs in a way that shouldn't matter, or one of the numbers is wrong.",
  "triggering_refs": {
    "chunks": ["c-0031", "c-0040", "c-0042"],
    "artifacts": ["writeups/exp_042.md"]
  }
}
```

**Field conventions:**

- **`smell`** — 1 line, ≤80 chars. The *claim*, not a summary of reasoning. Good: *"Seed labeled as fixed but 3 runs give different numbers."* Bad: *"I've been thinking about the runs and something seems off."* This is what the Scientist sees in the compact side-panel view and uses to gate.
- **`description`** — 2–4 sentences. Must cite *specific* prior evidence (chunk refs by ID and/or artifact paths by filename) that the current observation contradicts or strains against. No grounding → no hunch. This is what the Scientist reads on expand, and what gets prepended to the Researcher on inject.
- **`triggering_refs`** — machine-readable versions of the citations. Used by the framework (and future UI) to highlight the originating evidence.

**Deliberately absent from output:**

- **No `diagnostic` field.** Per VISION §Smell-don't-diagnose, the Critic flags the moment and stops there. Diagnosis happens downstream — Researcher reasons about it, Scientist joins if needed.
- **No `confidence` field in v0.** Tempting, but adds calibration work we can't do without data. If the Critic is uncertain, it should emit nothing. Binary gate.
- **No `who` field.** The Critic is the one raising this; that's the whole point. (Contrast with the offline miner, which extracts flags raised by humans or assistants.)

---

## Prompt design

**Ported from the v2 mining prompt at `agentic_research_critic/prompts/mining_prompt.md`, but the task framing is inverted:**

- **Mining prompt (offline):** *Given this conversation segment, identify instances where someone flagged a nose-firing moment.* Task = **recognize**.
- **Critic prompt (online):** *Given this recent conversation and current artifact state, raise a hunch if and only if a seasoned scientist would reflexively raise an eyebrow.* Task = **generate**.

### What ports cleanly

- **Calibration examples** — the 5+ real nose-firing examples from mining prompt (smooth-valley optimizer, MMLU-up-under-sycophancy, subset-containment violation, etc.). These remain excellent anchors for *what counts*.
- **Exclusion list** — "interesting" as filler, routine debugging, hyperparameter discussion, monitoring narration. These exclusions are calibration-critical and ported wholesale.
- **Sensitivity guidance** — questions that presuppose anomaly, directional violations, hunches followed by explanations still count.

### What changes

- **Task framing** rewritten: *"You are the Critic. Your job is to notice."* Not *"extract who noticed."*
- **"Who" / "signal_text" / "diagnostic" / "confidence" output fields removed.** Replaced with `smell` + `description` + `triggering_refs` as above.
- **Grounding discipline added explicitly**: the prompt must force the Critic to cite the earlier evidence its smell strains against. No cite → no emit.
- **Prior-hunches block added**: *"Here are hunches you've already raised. Do not re-raise the same concern, even phrased differently. If the Researcher's response since the hunch was raised resolved it, stay quiet. If they dodged or misunderstood, you may raise a follow-up, but make clear how the follow-up differs from the prior hunch."*
- **Cardinality enforced**: the prompt asks for 0 or 1 hunch. Multi-hunch output is permitted by the schema but discouraged in v0.
- **Bar language baked in**: *"Emit only what a seasoned scientist would reflexively raise an eyebrow at. When in doubt, stay quiet."* Matches VISION's framing of the asymmetry — missing a good hunch is recoverable, a stream of bad ones burns the Scientist's attention.

### Prompt template skeleton

The prompt file will live at `hunch/critic/prompts/nose_v0.md`. Shape:

```
[System / calibration block — mostly ported from v2]
  - What a nose-firing moment is
  - Calibration examples
  - Exclusion list
  - Sensitivity guidance

[Grounding discipline — new]
  - You must cite specific prior evidence
  - Cite by chunk_id or artifact path

[Task framing — new]
  - You are the Critic watching research in progress
  - Emit 0 or 1 hunch per tick
  - Bar: seasoned-scientist-eyebrow-raise

[Prior hunches — new, dynamically rendered]
  {prior_hunches_block}

[Recent conversation — dynamically rendered]
  {recent_chunks}

[Current artifacts — dynamically rendered]
  {artifacts_block}

[Output schema — new]
  - JSON: smell + description + triggering_refs
  - Or empty list for no hunch
```

---

## Context window

Each tick, the Critic sees:

- **Last 20 parsed chunks** from `conversation.jsonl`. Tunable via config. 20 chunks spans ~5–15 minutes of active work in the sibling projects, which seems roughly the right horizon for "colleague paying attention."
- **All `.md` artifacts** in the replay buffer, full content. Tunable; if total content exceeds a size budget (~20KB for v0), oldest-modified artifacts are dropped first.
- **Prior hunches** from the last M emitted, folded to current status. M = 10 for v0.
- **Feedback for those hunches** — good/bad/skip labels + implicit-feedback Scientist text.

**Excluded in v0:**

- Figures (`.png`/`.jpg`/etc.) — vision input is v0.5+. The artifact snapshots include figure files (framework captures them), but the Critic doesn't read them.
- Non-`.md` artifacts (CSVs, notebooks) — v0.5+.
- Bash output / raw tool results — by design. Per VISION §meeting-room-colleague, the Critic's resolution is "the summarized flow of the work," not every keystroke.

---

## Duplicate suppression

In-prompt, LLM-driven: the prompt includes a `prior_hunches` block, shows the Critic its recent past emissions + feedback, and instructs it not to re-raise the same concern. This is the simplest mechanism that works and it matches how a human colleague self-suppresses ("I already said this, let me not repeat myself").

**Not** embedding similarity, **not** fuzzy-matching, **not** any rule-based classifier. Those are over-engineering for v0 and would couple the framework to a specific Critic implementation.

Future Critics (agentic, with scratchpad) can maintain their own internal suppression state; the framework doesn't care.

---

## The "worth-interrupting" bar

Encoded in the prompt, not in code. Key language:

> *"The Scientist is working. Every hunch you raise costs their attention. Emit only what a seasoned scientist would reflexively raise an eyebrow at. When unsure, stay quiet. Missing a real hunch is recoverable; a stream of noise is not."*

**Why in the prompt, not a threshold/filter:**

- A post-hoc confidence filter requires calibrated confidence, which we don't have.
- A rate-limit (e.g., "max 1 hunch per 5 min") is a blunt instrument — it'd suppress a burst of legitimate follow-ups during a bad stretch.
- The prompt-level bar is tunable by the person iterating on the prompt, not by framework config.

For v0 we accept that "bar" is qualitative. If the stub is too noisy or too silent in practice, the fix is prompt iteration.

---

## Key design decisions (and why)

**C1: Stateless Sonnet, not agentic Claude Code.**
Stateless ships in a day or two; agentic needs tool setup, scratchpad design, and careful prompt engineering. For a v0 stub we don't need cross-tick memory — `prior_hunches` in the tick input gives weak coherence, which is enough to exercise the framework.

**C2: Smell + description with cited grounding.**
Resolves the side-panel-gate tension (we discussed this at length during design). Compact smell for quick gate, expandable description with citations answers "what does this contradict?" for ~80% of cases, conversation-via-replay-buffer handles the rest without explicit dialogue.

**C3: No diagnostic in the output.**
VISION §Smell-don't-diagnose is load-bearing. We're committing to this split publicly; breaking it here would be a scope expansion with real cost.

**C4: Grounding is required (no cite → no hunch).**
The failure mode we most fear from a stub Critic: vague pattern-matched "this feels off" hunches that waste the Scientist's attention. Requiring a cite to specific prior evidence is a cheap filter against this.

**C5: Prompt-level bar, not code filter.**
See § above. Keeps iteration speed high.

**C6: In-prompt dedup, not algorithmic.**
Simplicity; leaves the door open for stateful Critics that self-dedup via scratchpad.

**C7: Context window = last 20 chunks + full `.md` artifacts.**
Starting point. Will tune based on latency, cost, and quality. The numbers are config, not code.

---

## Deferred decisions

| Decision | Defer to | Revisit trigger |
|---|---|---|
| Vision input (figures) | v0.5 | Once a Scientist reports missing a hunch that needed a plot |
| Mentorship dialogue (pre-gate Q&A, post-miss elicitation) | v0.5 | Framework protocol already reserves `mentorship_tick` |
| Agentic Critic (long-running Claude Code with scratchpad) | v0.5+ | Framework protocol supports it as a drop-in process replacement |
| Cross-tick principle accumulation | v1 | Needs agentic Critic as substrate |
| Calibrated confidence scores | v1 | Needs feedback data to calibrate against |
| Fine-tuned model backend | v1+ | Needs training data |

---

## Implementation notes (for the builder)

- **Module layout:**
  ```
  hunch/critic/
    __init__.py
    stateless_sonnet.py   # the v0 implementation
    prompts/
      nose_v0.md          # ported + adapted prompt
  ```
- **Protocol adapter:** `stateless_sonnet.py` exposes `critic_tick(tick_input) -> tick_result` matching the framework's in-process call. A thin stdio wrapper (`hunch/critic/run.py`) can be added if/when we want subprocess isolation.
- **Sonnet call:** use the Anthropic SDK with prompt caching markers on the stable prefix (system + calibration block). Dynamic content (prior_hunches, recent chunks, artifacts) goes after the cache boundary.
- **Response parsing:** expect JSON output. If parsing fails, log and emit no hunch (don't crash the framework). The prompt should instruct *"output only valid JSON, no prose."*
- **Testing:** fixture-based unit tests for the prompt-renderer and response-parser. Integration test with a canned replay buffer is useful but not required for v0 merge.

---

*This doc is a starting point. Specific mechanisms (window size, bar language, prior-hunches block shape) will change as we iterate on real transcripts. The output schema and the prompt's task framing are what we expect to be durable.*
