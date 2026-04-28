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
  "ts": "2026-04-27T14:30:00Z"
}
```

- `bank_id`: monotonic `hb-NNNN`, allocated at ingest (scan file for max ID on startup).
- `canonical_smell` / `canonical_description`: the first occurrence's wording, never mutated.
- `source_run` / `source_hunch_id`: which run's hunch became the canonical entry.

### `link` — duplicate hunch mapped to an existing bank entry

Written at ingest when a hunch matches an existing bank entry (via LLM dedup judge), or manually by the evaluator in the annotation tool.

```json
{
  "type": "link",
  "bank_id": "hb-0001",
  "run": "sp_deepseek_run01",
  "hunch_id": "h-0011",
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
  "labeled_by": "ariel",
  "ts": "2026-04-28T10:00:00Z"
}
```

- `label`: `tp` | `fp` | `null`. `tp`/`fp` are substantive judgments. `null` is a retraction ("I withdraw my previous label"). No `skip` — skip is a UI-only workflow state, not persisted in the bank.
- `category`: optional free-text (e.g., `confound`, `measurement`, `contradiction`).
- `labeled_by`: evaluator identifier.
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

### Key properties

- **Last-write-wins** within a `(bank_id, run, hunch_id)` triple, by ts.
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

## Append monotonicity

Every event appended to `hunch_bank.jsonl` must have a `ts` strictly greater than the last event's `ts` in the file. The writer checks this before appending and raises `RuntimeError` if violated. This guards against clock skew or concurrent writers producing an ambiguous event order.

Same pattern as the hunch ID monotonicity guard in `HunchesWriter`.

## Bank ID allocation

Same pattern as hunch IDs: `hb-NNNN`, 4-digit zero-padded, monotonic. On startup, scan `hunch_bank.jsonl` for the highest existing ID number and allocate from there. Single-writer assumption (same as HunchesWriter).

## Open questions (to be resolved before implementation)

- **Sync semantics.** How does `hunch bank sync` discover eval dirs, detect new/interrupted ingests, and resume? Manifest-based or convention-based?
- **Dedup judge.** Reuse the existing Haiku dedup prompt (`judge_dedup.md`) or adapt it? Windowed comparison (as in `cross_run_dedup.py`) or compare each new hunch against the full bank?
- **Annotation tool integration.** How does `annotate_web.py` read/write the bank? What changes to the UI for inherited labels?
- **Feedback.jsonl coexistence.** Live feedback (`good`/`bad`/`skip` from the side panel during `hunch run`) still goes to `feedback.jsonl`. How does it relate to the bank? Ingested as labels at sync time?
