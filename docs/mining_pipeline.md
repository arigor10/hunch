# Mining Pipeline

**Status:** v1.0 design, 2026-05-13

Ground-truth hunches from historical transcripts. The mining pipeline extracts moments where the user noticed anomalies during a research session, locates the earliest conversation evidence that would let a critic catch the issue, and emits proper hunches with triggering refs.

## Why mine?

The critic eval loop needs ground truth: real concerns that a perfect critic *should* have caught. Three sources exist:

1. **Live labels** — the user presses "good" on a hunch during `hunch run`. Ecological but sparse.
2. **Retrospective annotation** — the user labels critic output after the fact. Deliberate but only covers what the critic *did* say.
3. **Mining** — extract what the user noticed *regardless* of whether the critic said anything. This is the recall denominator: the full set of catchable concerns.

Mining produces ground-truth hunches that enter the bank via `hunch bank sync`, participate in dedup matching against critic output, and propagate labels to future runs. See [hunch_bank_design.md § Mined hunches](hunch_bank_design.md#mined-hunches).

## Pipeline overview

Two commands, one human-readable intermediate file:

```
conversation.jsonl  →  findings.jsonl  →  hunches.jsonl  →  hunch bank sync

├── hunch mine nose ──┤├─ hunch mine evidence ─┤├── hunch bank sync ──┤
    find what the          find where the            dedup + label
    user noticed           evidence was              true positives
```

`findings.jsonl` is the seam between the two stages. Users who prefer to label moments by hand can skip `hunch mine nose` entirely and write `findings.jsonl` directly.

## CLI

### `hunch mine nose`

```bash
hunch mine nose \
  --project-dir ~/my-project \
  --output-dir ~/.hunch/mined/nose_v1 \
  --model claude-sonnet-4-5-20250929 \
  --window-size 200 \
  --overlap 50
```

Reads `.hunch/replay/conversation.jsonl`, chunks it into overlapping windows, sends each to the mining LLM, deduplicates across overlaps, and writes `findings.jsonl`.

### `hunch mine evidence`

```bash
hunch mine evidence \
  --project-dir ~/my-project \
  --findings .hunch/mined/nose_v1/findings.jsonl \
  --output-dir .hunch/mined/nose_v1
```

Reads `findings.jsonl` and the replay, runs an agent session per finding, writes `hunches.jsonl` ready for `hunch bank sync`. Supports checkpoint/resume (this is the expensive step).

## Stage 1: Nose mining

### What it finds

Explicit moments where the user questioned a result, flagged an inconsistency, or expressed suspicion — stated directly in the conversation text. The signal is a verbatim quote where the user's "nose fired."

v1.0 mines only user-raised, explicit anomaly flags. This deliberately excludes:

- **AI-raised findings** — the assistant flagging its own concerns. Useful but not ground truth for measuring whether a separate critic would have caught it.
- **Implicit detections** — silent course corrections where the user changed approach without stating why. Harder to mine reliably, requires inferring unstated intent.
- **Missed opportunities** — visible anomalies nobody remarked on. Requires the miner to independently understand the evidence, not just find where someone flagged it.

### Segmentation

The conversation (`conversation.jsonl`) is divided into fixed-size windows before being sent to the mining LLM. Each window must be large enough to provide context for anomaly detection but small enough to fit in a single LLM call.

**Algorithm:** fixed event-count windows (default ~200 events), breaking only at user turn boundaries (never mid-utterance). Each window records its `tick_seq` range so findings can be mapped back to the replay buffer.

**Overlap:** each window includes ~50 events of trailing context from the previous window. Without overlap, a nose moment at seq 202 referencing "what we saw earlier" at seq 198 would be context-free if the window started at seq 200. Findings that appear in both windows are deduplicated by `tick_seq` (same signal at the same seq = same finding).

### Mining LLM call

Each window is rendered as readable dialogue (user/assistant turns, artifact write/edit metadata) and sent to a capable LLM (Sonnet-class) with a mining prompt. The prompt:
- Defines what counts as an explicit nose-firing moment by the user
- Includes generic calibration examples covering common patterns (question-form anomaly detection, directional violations, subset-containment failures)
- Specifies sensitivity rules (e.g., questions presupposing anomalies count — "why would X go down?" is anomaly detection even if phrased politely)
- Defines what to exclude (operational/infrastructure, code-only, hypothesis falsification by designed test)
- Requests structured output per finding

The bundled prompt works out-of-the-box. For projects with known anchor cases, adding project-specific examples improves recall but is not required.

### Output: `findings.jsonl`

One JSON object per line:

```json
{"id": "NF-001", "tick_seq": 542, "signal_text": "wait, that can't be right — accuracy went up?", "anomaly": "Accuracy improved under an intervention designed to hurt it", "confidence": "high"}
{"id": "NF-002", "tick_seq": 871, "signal_text": "but we said we'd normalize by residual norms", "anomaly": "Raw norms compared across layers without normalization despite earlier commitment", "confidence": "high"}
```

Fields:
- `id` — unique identifier (NF-001, NF-002, ...)
- `tick_seq` — conversation event index where the signal text occurs
- `signal_text` — verbatim quote from the conversation
- `anomaly` — one-sentence description of what looked wrong
- `confidence` — `high` or `medium`

The rendered conversation labels each turn with its sequence number (e.g., `[Scientist] (seq 542): ...`), so the LLM reads `tick_seq` directly from context.

This file is the seam between stages. Users who don't want automated mining can write it by hand — just fill in the fields for each moment they remember noticing something off.

### Prompt design principles

1. **Calibrate against known cases (when available).** If the project has known anchor cases, test the prompt on segments containing them. Iterate until recall is 100%. Without anchors, the bundled generic examples provide a reasonable baseline.
2. **Explicit permission for edge cases.** LLMs are conservative by default — if questions-that-presuppose-anomalies should count, say so explicitly with an example.
3. **Quality over quantity.** The prompt should prefer 5 high-confidence findings over 20 medium ones. Ground truth with noise is worse than a smaller clean set.
4. **The anomaly description is the quality signal.** Concrete descriptions that name what's wrong ("accuracy goes up under an intervention designed to hurt it") indicate genuine understanding; vague ones ("something seems off") indicate pattern matching.

## Stage 2: Evidence mining

### Goal

For each finding, locate the **earliest point in the conversation** where enough evidence exists for a critic to raise the concern — *before* the user noticed it.

This stage also converts findings into proper hunches. The evidence agent writes the `smell` and `description`, because it has the full context needed (what the anomaly is, where the evidence is, how the dots connect). The nose mining stage only sees one window — it can say "something's wrong" but can't write a hunch that references evidence from hundreds of turns earlier.

### Algorithm

An agent (Claude with Read/Grep/Glob tools) is given:
- The full conversation history up to the signal turn (sliced from conversation.jsonl)
- Artifact snapshots as they existed at each point
- The finding's anomaly description and signal text

The agent searches for evidence: contradicting claims, forgotten commitments, results that conflict with earlier results, etc. It reports:

```json
{
  "evidence_tick_seqs": [143, 145, 152, 157],
  "earliest_raisable": 157,
  "artifacts": ["docs/exp_004_plan.md"],
  "evidence_summary": "how the evidence accumulates",
  "smell": "short description suitable for a hunch",
  "description": "fuller explanation of the concern"
}
```

### Output: `hunches.jsonl`

The evidence mining stage writes `hunches.jsonl` directly — no separate generation step. Each finding becomes a hunch event:

- `type: "emit"` — standard event type, compatible with all tooling
- `source: "mined"` — provenance marker
- `source_finding_id: "NF-001"` — traceability to original nose moment
- `emitted_by_tick: -1` — sentinel for "not produced by a critic tick"
- `bookmark_now` = `earliest_raisable` (when a critic could first fire)
- `bookmark_prev` = `bookmark_now - 1` (minimal window; evidence trail is in triggering_refs)
- `triggering_refs.tick_seqs` = `evidence_tick_seqs` (the full evidence trail)
- `triggering_refs.artifacts` = artifact paths the agent identified as evidence

Findings where the agent found no evidence are excluded (the concern isn't detectable from the conversation alone).

### Checkpoint/resume

Evidence mining is expensive (~$0.50/finding, ~500s/finding). The output file is written incrementally: after each finding completes, the result is appended. On restart, already-processed findings are skipped. Error entries (timeouts, failures) are re-processed on retry.

### Key design choices

- **Full history, not a window.** The agent sees everything up to the signal turn. Long-range evidence accumulation — the most interesting case for Hunch — would be missed by a narrow window.
- **Artifact access.** The agent can read artifact snapshots as they existed at each point. Some findings require seeing what was written in a plan doc vs. what was actually done.
- **Conservative timeout.** Evidence searches on long transcripts (1000+ turns) need generous timeouts (600s+).
- **Atomic writes with backup.** The output file is written via tmp+rename, with a `.bak` copy of the previous version before each write.

## Bank sync

Standard `hunch bank sync` discovers `.hunch/mined/*/hunches.jsonl` and processes them like any other run:
- Dedup-matches against existing bank entries
- Creates new entries or links to existing ones
- Auto-writes `label: "tp", labeled_by: "mined"` for each ingested hunch

Mined hunches are, by construction, true positives: the user actually noticed the concern, and the evidence mining confirmed it's detectable from the transcript.

## Cost model

| Stage | Cost per unit | Notes |
|-------|--------------|-------|
| Nose mining | ~$0.02/window | One Sonnet call per window (~5 findings/window) |
| Evidence mining | ~$0.50/finding | Agent session with tool use, full history access |
| Bank sync | ~$0.01/finding | Haiku dedup comparisons |

For a project with ~60 candidate findings: ~$30 total, dominated by evidence mining.

## Output location

```
<project>/.hunch/mined/<run_name>/findings.jsonl    # nose mining output
<project>/.hunch/mined/<run_name>/hunches.jsonl     # evidence mining output (bank-ready)
```

Run name identifies the mining batch (e.g., `nose_v1`, `nose_v2_full`). See [hunch_bank_design.md § Mined hunches](hunch_bank_design.md#mined-hunches) for disk layout and bank integration details.
