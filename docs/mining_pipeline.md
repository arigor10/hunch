# Mining Pipeline

**Status:** v0 draft, 2026-05-05

Ground-truth hunches from historical transcripts. The mining pipeline extracts moments where the Scientist noticed anomalies during a research session, locates the earliest conversation evidence that would let a critic catch the issue, and emits proper hunches with triggering refs.

## Why mine?

The critic eval loop needs ground truth: real concerns that a perfect critic *should* have caught. Three sources exist:

1. **Live labels** — the Scientist presses "good" on a hunch during `hunch run`. Ecological but sparse.
2. **Retrospective annotation** — the Scientist labels critic output after the fact. Deliberate but only covers what the critic *did* say.
3. **Mining** — extract what the Scientist noticed *regardless* of whether the critic said anything. This is the recall denominator: the full set of catchable concerns.

Mining produces ground-truth hunches that enter the bank via `hunch bank sync`, participate in dedup matching against critic output, and propagate labels to future runs. See [hunch_bank_design.md § Mined hunches](hunch_bank_design.md#mined-hunches).

## Pipeline overview

```
conversation transcript  →  nose mining  →  evidence mining  →  emit generation  →  bank sync
                            (find what       (find where         (write             (dedup +
                             was noticed)     evidence was)       hunches.jsonl)     label tp)
```

## Stage 1: Nose mining

### What it finds (v1.0 scope)

Explicit moments where the Scientist questioned a result, flagged an inconsistency, or expressed suspicion — stated directly in the conversation text. The signal is a verbatim quote where the Scientist's "nose fired."

v1.0 deliberately excludes:
- **Implicit detections** — silent course corrections where the Scientist changed approach without stating why. Harder to mine reliably, requires inferring unstated intent.
- **Missed opportunities** — visible anomalies nobody remarked on. Requires the miner to independently understand the evidence, not just find where someone flagged it.

These are candidates for v1.1 once the explicit pipeline is validated.

### Segmentation

The conversation (`conversation.jsonl`) is divided into segments before being sent to the mining LLM. Each segment must be large enough to provide context for anomaly detection but small enough to fit in a single LLM call.

**v1.0 algorithm:** fixed word-count windows with a configurable soft target (default TBD — likely in the 4K–10K word range), breaking only at user turn boundaries (never mid-utterance). Each segment records its `tick_seq` range so findings can be mapped back to the replay buffer.

**Overlap:** each segment includes trailing context from the previous segment (configurable, default ~1K words) so the LLM has enough context for findings near the start of a segment. Without overlap, a nose moment at seq 202 referencing "what we saw earlier" at seq 198 would be context-free if the segment started at seq 200. Findings that appear in both segments are deduplicated by `tick_seq` (same signal at the same seq = same finding).

The window size trades off context (larger = more cross-turn patterns visible) against focus and cost (smaller = LLM stays on task, cheaper per call). The right default will be calibrated against anchor cases before the first production run.

### Mining LLM call

Each segment is rendered as readable dialogue (user/assistant turns, artifact write/edit metadata) and sent to a capable LLM (Sonnet-class) with a mining prompt. The prompt:
- Defines what counts as an explicit nose-firing moment
- Includes generic calibration examples covering common patterns (question-form anomaly detection, directional violations, subset-containment failures)
- Specifies sensitivity rules (e.g., questions presupposing anomalies count — "why would X go down?" is anomaly detection even if phrased politely)
- Requests structured output per finding

The bundled prompt works out-of-the-box. For projects with known anchor cases, adding project-specific examples improves recall but is not required.

### Output per finding

```json
{
  "id": "NF-064",
  "who": "scientist",
  "type": "explicit",
  "signal_text": "verbatim quote from conversation",
  "anomaly": "one-sentence description of what looked wrong",
  "confidence": "high",
  "tick_seq": 157
}
```

The `tick_seq` is the conversation event index where the signal text occurs. The rendered conversation labels each turn with its sequence number (e.g., `[Scientist] (seq 157): ...`), so the LLM reads it directly. This makes the finding self-contained: the evidence mining stage uses `tick_seq` as its conversation cutoff without needing to know the segmentation strategy.

### What to exclude

The mining prompt defines what *not* to flag. These exclusion rules are part of the prompt itself, not a separate classification step:

- **Operational/infrastructure** — GPU issues, training duration, download speeds. Real catches but not scientific methodology; already caught by tooling.
- **Code-only** — anomalies discoverable only by reading source code, not from conversation or artifacts. Already caught by the AI assistant.
- **Hypothesis falsification** — a designed test returned a negative result. The experiment worked as intended; a negative finding is not an anomaly.

The target is scientific methodology anomalies: contradictions in results, forgotten commitments, flawed experimental designs, overlooked confounds. The prompt tells the LLM what this looks like *and* what it doesn't.

### Prompt design principles

1. **Calibrate against known cases (when available).** If the project has known anchor cases, test the prompt on segments containing them. Iterate until recall is 100%. Without anchors, the bundled generic examples provide a reasonable baseline.
2. **Explicit permission for edge cases.** LLMs are conservative by default — if questions-that-presuppose-anomalies should count, say so explicitly with an example.
3. **Quality over quantity.** The prompt should prefer 5 high-confidence findings over 20 medium ones. Ground truth with noise is worse than a smaller clean set.
4. **The anomaly description is the quality signal.** Concrete descriptions that name what's wrong ("accuracy goes up under an intervention designed to hurt it") indicate genuine understanding; vague ones ("something seems off") indicate pattern matching.

## Stage 2: Evidence mining

### Goal

For each nose-moment finding, locate the **earliest point in the conversation** where enough evidence exists for a critic to raise the concern — *before* the Scientist noticed it. This is also where the raw finding becomes a proper hunch: the evidence agent writes the `smell` and `description`, because it has the full context needed (what the anomaly is, where the evidence is, how the dots connect). The nose mining stage only sees one segment — it can say "something's wrong" but can't write a hunch that references evidence from hundreds of turns earlier.

This is distinct from where the Scientist raised the issue. The critic's value is catching things early; the evidence often appears turns or hundreds of turns before anyone remarks on it.

### Algorithm

An agent (Claude with Read/Grep/Glob tools) is given:
- The full conversation history up to the signal turn
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

### Key design choices

- **Full history, not a window.** The agent sees everything up to the signal turn. Long-range evidence accumulation — the most interesting case for Hunch — would be missed by a narrow window.
- **Artifact access.** The agent can read artifact snapshots as they existed at each point. Some findings require seeing what was written in a plan doc vs. what was actually done.
- **Conservative timeout.** Evidence searches on long transcripts (1000+ turns) need generous timeouts (600s+).

## Stage 3: Emit generation

Converts evidence mining results into `hunches.jsonl` events:

- `type: "emit"` — standard event type, compatible with all tooling
- `source: "mined"` — provenance marker
- `source_finding_id: "NF-064"` — traceability to original nose moment
- `emitted_by_tick: -1` — sentinel for "not produced by a critic tick"
- `bookmark_now` = `earliest_raisable` (when a critic could first fire)
- `bookmark_prev` = `bookmark_now - 1` (minimal window; evidence trail is in triggering_refs)
- `triggering_refs.tick_seqs` = `evidence_tick_seqs` (the full evidence trail)
- `triggering_refs.artifacts` = artifact paths the agent identified as evidence (e.g., plan docs, result tables)

Findings where the agent found no evidence are excluded (not detectable).

### Within-batch dedup

Before bank sync, mined hunches may optionally be dedup-filtered within the batch (using the same dedup judge prompt as the within-run filter). This catches cases where two nose moments describe the same underlying concern from different angles. In practice, nose moments mined from different conversation turns are almost always distinct, so this step can be skipped when the source findings were independently verified.

## Stage 4: Bank sync

Standard `hunch bank sync` discovers `.hunch/mined/*/hunches.jsonl` and processes them like any other run:
- Dedup-matches against existing bank entries
- Creates new entries or links to existing ones
- Auto-writes `label: "tp", labeled_by: "mined"` for each ingested hunch

Mined hunches are, by construction, true positives: the Scientist actually noticed the concern, and the evidence mining confirmed it's detectable from the transcript.

## Cost model

| Stage | Cost per finding | Notes |
|-------|-----------------|-------|
| Nose mining | ~$0.02 | One Sonnet call per segment (~5 findings/segment), scope exclusions inline |
| Evidence mining | ~$0.45 | Agent session with tool use, full history access |
| Bank sync | ~$0.01 | Haiku dedup comparisons |

For a project with ~60 candidate findings: ~$30 total, dominated by evidence mining.

## Output location

```
<project>/.hunch/mined/<run_name>/hunches.jsonl
```

Run name identifies the mining batch (e.g., `nose_v2_full`, `nose_v3_longrange`). See [hunch_bank_design.md § Mined hunches](hunch_bank_design.md#mined-hunches) for disk layout and bank integration details.
