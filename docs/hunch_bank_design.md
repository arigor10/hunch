# Hunch Bank Design

**Status:** v0 draft, 2026-04-27
**Supersedes:** Phase 2 of `eval_infrastructure.md` (label bank section)

## Purpose

The hunch bank is the canonical, project-level collection of unique concerns discovered by the Critic across all runs. It serves three roles:

1. **Identity layer.** Every emitted hunch maps to exactly one bank entry. Duplicate hunches across runs share a bank entry.
2. **Label store.** All labels (human and inherited) live in the bank. No per-run labels.jsonl.
3. **Dedup accelerator.** As the bank grows, new runs auto-match more hunches, reducing evaluator annotation burden.

## Key design decisions

### The bank is the single source of truth for labels

No `labels.jsonl` per run. Human labels are written directly to the bank. The annotation tool reads from and writes to the bank. A run directory contains only `hunches.jsonl` (emitted hunches).

**Rationale:** A single source of truth eliminates propagation bugs and stale mirrors. The previous design (eval_infrastructure.md Phase 2) had labels.jsonl + label_bank.jsonl with bidirectional sync — too much complexity for the same outcome.

### Bank entries are created at ingest, not at labeling

The bank is populated when a run is ingested, before any human labeling. Every emitted (non-filtered) hunch gets a bank entry or a link to an existing one. Labels are an overlay applied later.

**Rationale:** This means the dedup infrastructure works from day one. The bank is the canonical set of unique concerns; labels are metadata on top of that identity layer.

### Hunch content is copied into the bank directory

At ingest, each run's `hunches.jsonl` is copied into `bank/runs/<run_name>/hunches.jsonl`. Bank entries and links reference `{run, hunch_id}`, resolved against these local copies.

**Rationale:** If someone deletes or re-creates a run directory, the bank's copies survive. The bank is self-contained. Mild redundancy (two copies while the run exists) is acceptable.

### Lazy label inheritance via the label resolver

When the annotation tool displays a hunch, it resolves its label by looking up the bank — no pre-computed weak labels written anywhere. A **label resolver** function computes the effective label for any `(run, hunch_id)` pair. See [Label resolver algorithm](#label-resolver-algorithm) for the full spec and [Scenarios](#scenarios) for worked examples.

The annotation UI renders inherited labels distinctly (e.g., dimmed or with an "inherited" badge). The human can confirm, override, or ignore.

### First-label-is-canonical (v0)

When someone labels a hunch, and its bank entry has no prior labels, that label becomes canonical (inherited by all other linked hunches for that bank entry). If a later label on a different linked hunch disagrees, both are recorded — no conflict resolution UI in v0. Disagreements are data for later analysis.

### Labels are tp or fp only; skip is a UI-only state

The bank stores only substantive judgments: `tp` (true positive) or `fp` (false positive). `skip` ("I saw this but can't decide") is not written to the bank — it is a workflow state tracked locally by the annotation tool to distinguish "seen, no judgment" from "never seen." A skip does not supersede an inherited label.

### Manual dedup in the annotation tool

The evaluator can mark a hunch as a duplicate of another hunch in the same run (e.g., when the within-run dedup filter missed a match). In bank terms, this appends a `link` event connecting the hunch to the other hunch's bank entry. If the hunch previously had its own bank entry, that entry becomes orphaned (no live links) and is excluded from future matching.

This uses the same `link` event type as dedup-at-ingest, just triggered manually with `"source": "manual"`.

### Run deletion via tombstoning

`hunch bank drop --run <name>` appends a tombstone event. Tombstoned runs are excluded from dedup matching, label resolution, and UI display. The bank remains append-only.

**Surviving links.** If a bank entry was created from run A (its canonical source), and run B also links to it, tombstoning run A does not delete the bank entry. The entry's canonical_smell and canonical_description are stored directly in the `entry` event — they don't depend on the source run's files. Run B's link remains live, and the bank entry continues to participate in future dedup matching.

**Orphaned entries.** If a bank entry's source run is tombstoned and no other non-tombstoned runs link to it, the entry becomes dormant: it still exists for dedup matching (the canonical wording is self-contained in the entry event), but there are no hunches to display in the annotation tool. If a future run rediscovers the same concern, it links to the dormant entry, reviving it.

**Canonical label after tombstoning.** If the human label that was canonical came from a tombstoned run, it remains canonical — labels are facts about what a human judged, not about the run's validity. However, the annotation tool can surface this for review (e.g., "canonical label from tombstoned run sp_garbage_run").

## Location

```
<project>/.hunch/bank/
  hunch_bank.jsonl              # event-sourced bank (append-only)
  runs/
    sp_sonnet_run01/
      hunches.jsonl             # copied at ingest
    sp_deepseek_run01/
      hunches.jsonl             # copied at ingest
```

Eval run directories live under `<project>/.hunch/eval/`:

```
<project>/.hunch/eval/
  sp_sonnet_run01/
    hunches.jsonl               # produced by critic run
  sp_deepseek_run01/
    hunches.jsonl
```

Live hunches from `hunch run` sessions live under `<project>/.hunch/replay/`:

```
<project>/.hunch/replay/
  hunches.jsonl                 # live critic output (grows each session)
  feedback.jsonl                # TUI labels (good/bad/skip)
  conversation.jsonl
  artifacts.jsonl
```

Live hunches are synced into the bank under the synthetic run name `:live` (colon is forbidden in directory names, preventing collision with eval run names). Unlike eval runs, the replay buffer is not copied — the original is the canonical artifact. See [Live hunches and feedback labels](#live-hunches-and-feedback-labels).

The bank is discovered automatically by the CLI (walk up to find `.hunch/`). No `--bank` flag.

## Bank event schema

The bank is an append-only JSONL file (`hunch_bank.jsonl`). Current state is derived by folding events in file order. Four event types:

### `entry` — new unique concern

Written at ingest when a hunch has no match in the existing bank.

```json
{
  "type": "entry",
  "bank_id": "hb-0001",
  "canonical_smell": "4-bit + SDPA diagnosed broken in exp-004 but relied on as working in exp-006",
  "canonical_description": "In c-0612, the scientist removed attn_implementation='eager'...",
  "source_run": "sp_sonnet_run01",
  "source_hunch_id": "h-0003",
  "bookmark_now": 42,
  "ts": "2026-04-27T14:30:00Z"
}
```

- `bank_id`: monotonic `hb-NNNN`, allocated at ingest (scan file for max ID on startup).
- `canonical_smell` / `canonical_description`: the first occurrence's wording, never mutated.
- `source_run` / `source_hunch_id`: which run's hunch became the canonical entry.
- `bookmark_now`: replay-buffer position where the hunch was emitted. Used for windowed dedup matching at sync time.

### `link` — duplicate hunch mapped to an existing bank entry

Written at ingest when a hunch matches an existing bank entry (via LLM dedup judge), or manually by the evaluator in the annotation tool.

```json
{
  "type": "link",
  "bank_id": "hb-0001",
  "run": "sp_deepseek_run01",
  "hunch_id": "h-0011",
  "bookmark_now": 42,
  "judge_score": 0.87,
  "source": "ingest",
  "replaces_bank_id": null,
  "ts": "2026-04-28T09:15:00Z"
}
```

- `judge_score`: the LLM judge's confidence (null when `source` is `"manual"`).
- `source`: `"ingest"` (automatic dedup at sync time) or `"manual"` (evaluator marked as duplicate in annotation tool).
- `replaces_bank_id`: when a manual dedup re-maps a hunch from one bank entry to another, this records the previous bank entry. Null for initial mappings. Captures the full action in one event (moved from hb-X to hb-Y) and keeps fold logic simple (last `link` per `(run, hunch_id)` wins).
- The linked hunch's full content is in `bank/runs/<run>/hunches.jsonl`.

### `label` — human judgment

Written by the annotation tool when a human labels a hunch.

```json
{
  "type": "label",
  "bank_id": "hb-0001",
  "run": "sp_sonnet_run01",
  "hunch_id": "h-0003",
  "label": "tp",
  "category": "confound",
  "labeled_by": "scientist_retro",
  "ts": "2026-04-28T10:00:00Z"
}
```

- `label`: `tp` | `fp` | `null`. `tp`/`fp` are substantive judgments. `null` is a retraction ("I withdraw my previous label"). No `skip` — skip is a UI-only workflow state, not persisted in the bank.
- `category`: optional free-text (e.g., `confound`, `measurement`, `contradiction`).
- `labeled_by`: label provenance channel. Known values:
  - `scientist_retro` — deliberate annotation in the annotation UI (highest confidence).
  - `operational_live` — live feedback from the side-panel TUI during `hunch run` (ecological validity, lower deliberative confidence). See [Live hunches and feedback labels](#live-hunches-and-feedback-labels).
  - `anchor` — hand-curated known-good catches.
  - `mined` — automated transcript mining.
  - `legacy_migration` — migrated from a pre-bank `labels.jsonl` without a source field.
- `run` + `hunch_id`: which specific linked hunch was labeled. See [Label resolver algorithm](#label-resolver-algorithm) for how labels are resolved.

### `tombstone` — run dropped

Written by `hunch bank drop --run <name>`.

```json
{
  "type": "tombstone",
  "run": "sp_garbage_run",
  "reason": "garbage run, discard",
  "ts": "2026-04-29T12:00:00Z"
}
```

## Derived state

Folding the event stream produces:

- **Bank entries**: `dict[bank_id → {canonical_smell, canonical_description, source_run, source_hunch_id, links: [...], labels: [...]}]`
- **Run index**: `dict[run_name → {tombstoned: bool, hunch_ids: [...]}]`
- **Hunch-to-bank mapping**: `dict[(run, hunch_id) → bank_id]` — last `link` event per `(run, hunch_id)` wins, handling manual re-mappings naturally.

All mappings are derived from the bank at load time. No separate mapping files.

## Label resolver algorithm

The label resolver computes the effective label for a given `(run, hunch_id)`. This is the single function that the annotation tool (and any reporting code) calls to determine what label to display.

### Inputs

The resolver takes the folded bank state and a `(run, hunch_id)` pair.

### Steps

1. **Resolve bank_id.** Find the current bank entry for this hunch: the last `link` event for `(run, hunch_id)`, or the `entry` event if this hunch is a canonical source. If the run is tombstoned, return `not_displayable`.

2. **Compute effective local label.** Collect all `label` events matching `(bank_id, run, hunch_id)`. Take the last by ts. If `label` is `tp` or `fp`, the effective local label is that value. If `label` is `null` (retraction), or no label events exist, the effective local label is absent.

3. **If local label exists** → return `{label: X, source: "human", category: ...}`.

4. **Compute inherited label.** Collect all `label` events for this `bank_id` across all non-tombstoned `(run, hunch_id)` pairs. For each pair, compute its effective label (last event wins; null = retracted = absent). Among all pairs that have a non-null effective label, pick the one whose earliest label event has the lowest ts — this is the canonical labeler. Return `{label: X, source: "inherited", inherited_from_run: Y, inherited_from_hunch_id: Z}`.

5. **If no inherited label** → return `{label: null, source: "unlabeled"}`.

### Label tiers

Labels are partitioned into tiers by `labeled_by`:

- **Tier 1 (deliberate):** `scientist_retro`, `anchor`, `mined`, `legacy_migration`, or any value not in the weak set.
- **Tier 2 (weak):** `operational_live` — live feedback from the TUI.

Tier ranking affects both local and inherited label resolution:

- **Local:** If a `(bank_id, run, hunch_id)` triple has both tier 1 and tier 2 labels, only tier 1 labels are considered. Within the same tier, last-write-wins by ts. This means an evaluator's deliberate label outranks a scientist's quick TUI feedback, regardless of timestamp order.
- **Inherited:** Among all linked hunches with a non-null effective label, partition by tier of the effective label's `labeled_by`. Pick from tier 1 first (earliest-first-label-ts). Only fall back to tier 2 if no tier 1 labels exist.

### Key properties

- **Last-write-wins** within a `(bank_id, run, hunch_id)` triple, by ts (within the same tier).
- **Tier 1 outranks tier 2.** A deliberate evaluator label always beats live feedback, regardless of timestamp.
- **Human beats inherited.** A local label always overrides an inherited one.
- **Retraction is local.** Retracting a label on one linked hunch does not affect labels on other linked hunches. If the retracted label was the canonical one, the next-earliest non-retracted label becomes canonical. If all labels are retracted, the bank entry reverts to unlabeled.
- **Relinking orphans old labels.** When a hunch is manually relinked from hb-X to hb-Y, any label events keyed to `(hb-X, run, hunch_id)` are orphaned — they apply to a mapping that no longer exists. The hunch's effective label is now resolved under hb-Y.

## Scenarios

Each scenario describes an action sequence and the expected label resolver output for each hunch.

### S1: Fresh hunch, no label

- Ingest run01: h-0001 → creates hb-0001 (entry)
- **Resolve (run01, h-0001):** `unlabeled`

### S2: Fresh hunch, labeled tp

- Ingest run01: h-0001 → hb-0001
- Human labels (hb-0001, run01, h-0001) as tp
- **Resolve (run01, h-0001):** `tp, source: human`

### S3: Re-label tp → fp

- Ingest run01: h-0001 → hb-0001
- Human labels tp
- Human labels fp (later ts)
- **Resolve (run01, h-0001):** `fp, source: human` (last-write-wins)

### S4: Label then retract

- Ingest run01: h-0001 → hb-0001
- Human labels tp
- Human retracts (label: null, later ts)
- **Resolve (run01, h-0001):** `unlabeled` (retraction clears the label)

### S5: Inherited label from another run

- Ingest run01: h-0001 → hb-0001
- Ingest run02: h-0005 links to hb-0001 (duplicate)
- Human labels (hb-0001, run01, h-0001) as tp
- **Resolve (run01, h-0001):** `tp, source: human`
- **Resolve (run02, h-0005):** `tp, source: inherited, from run01/h-0001`

### S6: Inherited label, human overrides

- Ingest run01: h-0001 → hb-0001
- Ingest run02: h-0005 links to hb-0001
- Human labels (hb-0001, run01, h-0001) as tp
- Human labels (hb-0001, run02, h-0005) as fp
- **Resolve (run01, h-0001):** `tp, source: human`
- **Resolve (run02, h-0005):** `fp, source: human` (local override beats inherited)

### S7: Label, then manually relink to different bank entry

- Ingest run01: h-0001 → hb-0001, h-0003 → hb-0003
- Human labels (hb-0001, run01, h-0001) as tp
- Human labels (hb-0003, run01, h-0003) as fp
- Evaluator manually relinks h-0001 to hb-0003 (link event with replaces_bank_id: hb-0001)
- **Resolve (run01, h-0001):** `fp, source: inherited, from run01/h-0003` — the tp label under hb-0001 is orphaned; h-0001 now lives under hb-0003 and inherits its fp label
- **Resolve (run01, h-0003):** `fp, source: human` — unchanged

### S8: Manual relink, then undo

- Ingest run01: h-0001 → hb-0001
- Human labels (hb-0001, run01, h-0001) as tp
- Evaluator manually relinks h-0001 to hb-0003 (link, replaces_bank_id: hb-0001)
- **Resolve (run01, h-0001):** resolved under hb-0003 (tp label orphaned under hb-0001)
- Evaluator undoes: relinks h-0001 back to hb-0001 (link, replaces_bank_id: hb-0003)
- **Resolve (run01, h-0001):** `tp, source: human` — back to original; the tp label event under hb-0001 is live again

### S9: Canonical label retracted, other hunches had inherited

- Ingest run01: h-0001 → hb-0001
- Ingest run02: h-0005 links to hb-0001
- Human labels (hb-0001, run01, h-0001) as tp ← canonical
- **Resolve (run02, h-0005):** `tp, source: inherited`
- Human retracts (hb-0001, run01, h-0001) (label: null)
- **Resolve (run01, h-0001):** `unlabeled`
- **Resolve (run02, h-0005):** `unlabeled` — the only label for hb-0001 was retracted, so no canonical label exists

### S10: Canonical retracted, but another linked hunch was also labeled

- Ingest run01: h-0001 → hb-0001
- Ingest run02: h-0005 links to hb-0001
- Human labels (hb-0001, run01, h-0001) as tp ← canonical
- Human labels (hb-0001, run02, h-0005) as fp ← override
- Human retracts (hb-0001, run01, h-0001)
- **Resolve (run01, h-0001):** `fp, source: inherited, from run02/h-0005` — run02's fp is now the only non-retracted label for hb-0001, so it becomes canonical
- **Resolve (run02, h-0005):** `fp, source: human`

### S11: Tombstoned run, canonical label survives

- Ingest run01: h-0001 → hb-0001
- Human labels (hb-0001, run01, h-0001) as tp
- Ingest run02: h-0005 links to hb-0001
- Tombstone run01
- **Resolve (run01, h-0001):** `not_displayable` (tombstoned)
- **Resolve (run02, h-0005):** `tp, source: inherited, from run01/h-0001` — label survives tombstoning

### S12: Dormant bank entry revived by new run

- Ingest run01: h-0001 → hb-0001
- Human labels (hb-0001, run01, h-0001) as tp
- Tombstone run01 → hb-0001 is dormant (no live links)
- Ingest run03: h-0009 links to hb-0001 (rediscovered the same concern)
- **Resolve (run03, h-0009):** `tp, source: inherited, from run01/h-0001` — dormant entry revived, label inherited

### S13: Three runs, disagreement

- Ingest run01: h-0001 → hb-0001
- Ingest run02: h-0005 links to hb-0001
- Ingest run03: h-0009 links to hb-0001
- Human labels (hb-0001, run01, h-0001) as tp ← canonical
- Human labels (hb-0001, run02, h-0005) as fp ← override
- **Resolve (run01, h-0001):** `tp, source: human`
- **Resolve (run02, h-0005):** `fp, source: human`
- **Resolve (run03, h-0009):** `tp, source: inherited, from run01/h-0001` — inherits canonical (earliest-labeled)

### S14: Live feedback label overridden by evaluator

- Ingest `:live`: h-0001 → hb-0001
- Scientist presses "good" in TUI → label event: (hb-0001, :live, h-0001) = tp, labeled_by = operational_live
- Evaluator labels (hb-0001, :live, h-0001) as fp, labeled_by = scientist_retro
- **Resolve (:live, h-0001):** `fp, source: human` — tier 1 (scientist_retro) outranks tier 2 (operational_live), regardless of timestamp order

### S15: Inherited tier ranking — evaluator label preferred over live feedback

- Ingest `:live`: h-0001 → hb-0001
- Ingest run02: h-0005 links to hb-0001
- Scientist presses "good" on :live/h-0001 → label: tp, labeled_by = operational_live (first label by ts)
- Evaluator labels (hb-0001, run02, h-0005) as fp, labeled_by = scientist_retro (later label by ts)
- **Resolve (:live, h-0001):** `tp, source: human` — local label (operational_live tier 2) is still a local label, so it takes priority over inheritance
- **Resolve (run02, h-0005):** `fp, source: human` — local evaluator label
- Now, add a third link: ingest run03: h-0009 links to hb-0001 (no local label)
- **Resolve (run03, h-0009):** `fp, source: inherited, from run02/h-0005` — tier 1 label (scientist_retro) is preferred for inheritance, even though the operational_live label was earlier by ts

## Append monotonicity

Every event appended to `hunch_bank.jsonl` must have a `ts` strictly greater than the last event's `ts` in the file. The writer checks this before appending and raises `RuntimeError` if violated. This guards against clock skew or concurrent writers producing an ambiguous event order.

Same pattern as the hunch ID monotonicity guard in `HunchesWriter`.

## Bank ID allocation

Same pattern as hunch IDs: `hb-NNNN`, 4-digit zero-padded, monotonic. On startup, scan `hunch_bank.jsonl` for the highest existing ID number and allocate from there. Single-writer assumption (same as HunchesWriter).

## Sync operation

`hunch bank sync` ingests eval runs into the bank. It discovers runs, dedup-matches new hunches against existing bank entries, creates entries and links, and optionally migrates legacy `labels.jsonl` files.

### Discovery

Sync scans `<project>/.hunch/eval/` for directories matching the pattern `<run_name>/hunches.jsonl`. The directory name is the run name. No recursive scan — only immediate children of `eval/`.

### Per-run sync flow

For each discovered run:

1. **Already ingested?** Check if `bank/runs/<run_name>/hunches.jsonl` exists.
   - **Yes → conflict check.** Compare the eval dir's `hunches.jsonl` against the bank's copy by `(hunch_id, smell)` tuples. If they differ, warn and skip:
     ```
     WARNING: sp_sonnet_run01/hunches.jsonl has changed since ingestion.
     To replace: `hunch bank drop --run sp_sonnet_run01`, then re-sync.
     To keep both: rename the eval dir, then re-sync.
     Skipping.
     ```
   - **Yes, same → resume check.** Compare emitted hunch IDs in the file against those already in the bank for this run. Process only missing ones (interrupted ingestion).
   - **No → fresh ingest.** Copy `hunches.jsonl` to `bank/runs/<run_name>/`, then process all emitted hunches.

2. **Dedup matching.** For each new (unprocessed) hunch, determine if it matches any existing bank entry. See [Dedup strategy](#dedup-strategy).

3. **Write events.** For matched hunches: write `link` events. For unmatched hunches: allocate `hb-NNNN` and write `entry` events.

4. **Legacy labels.jsonl migration.** If `labels.jsonl` exists in the eval dir, prompt the user. See [Legacy labels migration](#legacy-labels-migration).

### Dedup strategy

**Windowed comparison (Option A):** Loop over all non-tombstoned bank entries. For each entry, use its `bookmark_now` to find the ±k nearest hunches in the new run (by `bookmark_now`, via bisect). Compare each pair using the existing Haiku dedup prompt (`judge_dedup.md`). Default k=5 → up to 10 comparisons per bank entry.

**Cost:** `n_bank × 2k` LLM calls. For 150 bank entries × 10 = 1,500 calls at ~$0.10, <2 min with 10 parallel workers. Scales linearly with bank size.

**Why not full comparison:** At 150 bank × 100 new = 15,000 calls, full comparison is 10× more expensive for marginal recall improvement. The windowed approach catches duplicates that occur near the same point in the transcript, which covers the vast majority of cases (same concern triggered by the same evidence).

**bookmark_now on events:** Both `entry` and `link` events carry a `bookmark_now` field recording the replay-buffer position where the hunch was emitted. This enables the windowed comparison.

**Result processing:** After all comparisons, each new hunch has zero or more matches. If matched to one or more bank entries, link to the highest-scoring match. If unmatched, create a new entry.

### Legacy labels migration

When sync finds a `labels.jsonl` in an eval dir, it prompts the user:

```
Found labels.jsonl in sp_sonnet_run01/ (14 labels).
Migrating labels to the hunch bank.
See: docs/hunch_bank_design.md

Labels will be ingested and the file backed up as labels.jsonl.bak

[y] Continue  [n] Abort  [s] Skip this file

To exclude this run entirely, move it out of .hunch/eval/
```

On confirmation:
1. Copy `labels.jsonl` → `labels.jsonl.bak` (fault tolerance).
2. For each label: look up the hunch's `bank_id` from the bank mapping, write a `label` event to the bank with `labeled_by` from the original `source` field (or `"legacy_migration"` if absent).
3. Rename `labels.jsonl` → `labels.jsonl.bak` (marks as processed).

Pass `--yes` to auto-confirm all migrations (non-interactive).

### Idempotency and resumability

- Running sync twice with no changes = no-op.
- Interrupted sync: next run detects partially-ingested hunches (some IDs present, others not) and processes only the missing ones. The `hunches.jsonl` copy is written at the start, so it's always complete.
- A `labels.jsonl.bak` file signals that migration already completed for that run.

## Live hunches and feedback labels

Live hunches from `hunch run` sessions (stored in `.hunch/replay/hunches.jsonl`) are synced into the bank alongside eval runs. The synthetic run name `:live` prevents collision with eval run names (colon is forbidden in directory names).

### Discovery

Sync auto-discovers live hunches at `<bank_dir>/../replay/hunches.jsonl` (derived from the bank's location — no extra config). If the file exists, it's included in the sync.

### Incremental ingest

Unlike eval runs, live hunches are **not copied** into `bank/runs/`. The replay buffer is the original, append-only artifact — copying it would be pure redundancy with no conflict-detection benefit (eval copies exist because eval outputs might be re-extracted or deleted).

Each sync finds which `:live` hunch IDs are already in the bank (via `_already_ingested_ids`) and only dedup-matches + ingests the new ones. The existing resume mechanism handles interrupted syncs natively — no new code path needed.

### Feedback label import

After ingesting live hunches, sync reads `.hunch/replay/feedback.jsonl` and reconciles explicit labels with the bank:

1. Read all explicit labels from `feedback.jsonl` (last-write-wins per hunch_id, same as `read_labeled_hunch_ids()`).
2. Map vocabulary: `good` → `tp`, `bad` → `fp`. `skip` is ignored (UI-only, per bank convention). `implicit` channel events are ignored.
3. For each mapped label, check if the bank already has an `operational_live` label for `(:live, hunch_id)`:
   - **No existing label:** write a new `label` event with `labeled_by: "operational_live"`.
   - **Existing label differs:** write a new `label` event (scientist changed their mind).
   - **Existing label matches:** skip (idempotent).

`feedback.jsonl` is **never deleted or renamed** — it's a living file that keeps growing. The bank labels with `labeled_by: "operational_live"` are a reconciled projection of `feedback.jsonl`, updated each sync.

### Weakness semantics

Live feedback labels use `labeled_by: "operational_live"`, which is tier 2 (weak) in the label resolver. This means:

- An evaluator's `scientist_retro` label always outranks `operational_live`, regardless of timestamp.
- For inheritance, tier 1 labels are preferred. A live feedback label only propagates as an inherited label if no tier 1 labels exist for that bank entry.

This reflects the reality that pressing "good" in the TUI is a quick in-the-moment reaction, while retrospective annotation is a deliberate judgment.

## Open questions (deferred)

- **Annotation tool integration.** How does `annotate_web.py` read/write the bank? What changes to the UI for inherited labels?
