# Eval Infrastructure Design

**Status:** v0 draft, 2026-04-16 (updated 2026-04-22)
**Goal:** A lightweight eval loop that makes it painless for any [Scientist](../VISION.md#the-minimal-team) to run a critic version on their historical data, label the results, and share stats — without sharing raw research content.

## Motivation

Improving the Critic requires measuring it, and measuring it requires ground truth. The raw material — past research sessions — is plentiful but inert: replaying the Critic over a transcript (*replay data*) tells us what it *would have said*, not whether those catches are real. Only the Scientist who lived through the session can judge that, and Scientist time is scarce. So the eval problem reduces to: make it as effortless as possible for the Scientist to provide labels, persist those labels so they never need to be provided twice, and turn them into precision/recall numbers that guide the next iteration of the Critic.

## The flywheel

```
run critic  →  annotate hunches  →  shareable report  →  iterate on critic
     ↑                                                          │
     └──────────────────────────────────────────────────────────┘
```

The annotation tool is the bottleneck. If labeling is painful, nobody does it. If it's fast and contextual, we get labeled data, which lets us measure, which lets us improve.

## Data flow

```
.hunch/replay/ ──→ critic ──→ hunches.jsonl
                              │
                              ▼
                    novelty filter            ←── drop hunches the
                    (judge_novelty)               researcher/scientist
                              │                   already raised
                              ▼
                    label bank match          ←── project-level bank of
                    (semantic similarity)         previously-labeled
                              │                   hunches (with content)
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         auto-tp         auto-fp         unlabeled
              │               │               │
              │               │               ▼
              │               │       annotation UI ←── Scientist
              │               │               │       labels only
              │               │               │       genuinely new
              └───────────────┴───────────────┘
                              │
                              ▼
                       labels.jsonl  ──→ append to label bank
                              │              (grows over time)
                              ▼
                       eval_report.json  ←── shareable
                        (no raw content)
```

**The flywheel:** each session is shorter than the last. As the bank accumulates, more hunches auto-match and the Scientist only sees genuinely new concerns. This makes labeling rewarding (no repeats) and keeps precision measurement consistent across runs (the same concern always gets the same label).

## Replay format

The eval infra reads from the [`.hunch/replay/` directory layout](framework_v0.md#appendix-a-replay-buffer-schemas) — the same format that `hunch run` writes during a live session. The replay buffer is the **input** — read-only for eval purposes:

- `conversation.jsonl` — append-only event log with monotonic `tick_seq` numbers. Each line is a typed event (`user_text`, `assistant_text`, `artifact_write`, `artifact_edit`, `figure`, `claude_stopped`, etc.) with a timestamp.
- `artifacts.jsonl` — index of artifact write/edit events with paths, used for snapshot reconstruction.
- `artifacts/` — directory of artifact content snapshots.

Each eval run writes its **output** to a separate run directory (e.g. `.hunch/eval/sonnet_v1_run03/`):

- `hunches.jsonl` — hunches emitted by this critic version on this replay.
- `labels.jsonl` — Scientist labels for those hunches (see [Storage](#storage-labelsjsonl)).

This separation lets multiple critic versions be evaluated against the same replay data without clobbering each other. For retrospective eval on historical data, `hunch/parse/transcript.py:parse_whole_file` can parse a Claude Code transcript into the replay format.

**Resumability:** Both online (`hunch run`) and offline (`hunch replay-offline`) write `checkpoint.json` after each tick. On restart, the pipeline resumes from where it left off rather than reprocessing from scratch. For offline eval, the checkpoint lives in `--output-dir`; re-running against a partially-completed output directory continues the run.

## Trigger policy and novelty filter

Offline eval uses the same trigger policy and novelty/dedup filter as the live pipeline — see [framework_v0.md §2 Trigger](framework_v0.md#2-trigger) and [§4 Filter](framework_v0.md#4-filter-novelty--dedup). For offline replay, `claude_stopped` events are synthesized at speaker boundaries so the Critic fires at the same moments it would have fired live. This is what makes offline eval a faithful approximation of live behavior.

**Implications for the annotation annotation UI.** The same divider should appear in the human-labeling UI for the same reason — without it, "novel vs redundant" becomes a scan of the whole transcript. Labeler productivity is the flywheel bottleneck; any aid to orientation compounds.

## Storage: labels.jsonl

One file per run directory, append-only JSONL. Each line:

```json
{
  "hunch_id": "h-0003",
  "label": "tp",
  "category": "confound",
  "source": "scientist",
  "bank_match": null,
  "note": "optional free-text, stays local",
  "ts": "2026-04-16T09:30:00Z"
}
```

**Fields:**
- `hunch_id` — matches hunches.jsonl
- `label` — `tp` | `fp` | `skip` (skip = can't tell / not enough context)
- `category` — optional, free-text. Examples: `confound`, `measurement`, `contradiction`, `procedural`. Helps the report break down what the critic catches.
- `source` — `scientist` (labeled in annotation UI) | `bank` (auto-matched from label bank)
- `bank_match` — when `source: bank`, the `bank_id` that matched; null otherwise
- `note` — optional. For the Scientist's own reference. Stays local, never in the report.
- `ts` — ISO timestamp

**Re-labeling:** Append a new line with the same hunch_id. Last-write-wins (latest ts is canonical). No deletions — the file is an audit trail.

**Bank-sourced labels are revocable:** if the Scientist opens the annotation UI and disagrees with an auto-match, their override is appended (source: scientist) and wins. The bank entry that triggered the bad match should be flagged for review.

## The label bank

`.hunch/label_bank.jsonl` — project-level append-only store of every labeled hunch, with content. Lives alongside `.hunch/replay/` but outside any individual run directory. This is what makes the flywheel work.

Each entry:

```json
{
  "bank_id": "lb-0042",
  "label": "tp",
  "category": "confound",
  "smell": "4-bit + SDPA diagnosed broken in exp-004 but relied on as working in exp-006",
  "description": "In c-0612, the scientist removed attn_implementation='eager'...",
  "source_run": "ar_v1_run03",
  "source_hunch_id": "h-0003",
  "labeled_by": "operational_live",
  "ts": "2026-04-16T09:30:00Z",
  "matched_by": [
    {"run": "ar_v1_run07", "hunch_id": "h-0011", "judge_score": 0.87, "ts": "2026-05-03T..."},
    {"run": "ar_v2_run01", "hunch_id": "h-0002", "judge_score": 0.91, "ts": "2026-05-20T..."}
  ]
}
```

**Why content lives in the bank:** matching is by semantic similarity (smell + description). We need the text to compare new hunches against. The bank stays local — it never goes into the shareable report.

**Matching against the bank** (before annotation UI):
An LLM judge compares each new hunch against each bank entry's canonical smell+description. Above a similarity threshold, the new hunch inherits the bank entry's label automatically. The annotation UI only shows what didn't auto-match.

**When a match fires:** append a record to the bank entry's `matched_by` list (run, hunch_id, judge score, timestamp). The bank entry's canonical wording does not change. The matched hunch inherits the label via `source: bank` in labels.jsonl.

**When the Scientist labels a genuinely new hunch in annotation UI:** a new entry is appended to both `labels.jsonl` (per-run) and the bank (project-level) as a new `bank_id`.

### Why record match history but not promote alternatives

When a new hunch matches an existing bank entry, we could either (1) just record the match, or (2) add the new hunch's wording as an alternative formulation of the same concern, used for future matching.

Option 2 is tempting — it hedges against overindexing on whichever wording happened to be captured first. But it risks **cluster drift**: alt N is similar to alt N-1, alt N-1 to alt N-2, but alt N is no longer similar to canonical. Over many runs, everything starts matching everything.

The chosen middle path: **record match history, don't auto-promote.** The `matched_by` field captures which hunches across which runs matched each bank entry, but matching itself still uses one canonical wording per concern. This gives us:

- **No proliferation** — cluster shape stays bounded to one entry per concern
- **Audit value** — "this concern was re-discovered 7 times across 3 critic versions" is a useful signal in its own right
- **Evidence for later upgrades** — if we ever find matching is brittle, we have raw data to decide *which* alternative wordings to promote, rather than guessing

If paraphrases later prove necessary, the annotation UI can add an opt-in affordance: when the Scientist confirms an auto-match, prompt "promote this wording as an alternative formulation?" — keeps a human in the loop on bank vocabulary, prevents silent drift.

**Cross-Scientist caveat:** if multiple Scientists contribute labels, `labeled_by` is preserved (`operational_live`, `scientist_retro`, `anchor`, `mined`). Disagreements (same concern labeled tp by one, fp by another) become a separate review queue rather than auto-applying. v0 assumes a single Scientist per project.

## Ground truth

The label bank is the primary ground-truth mechanism. Labeled hunches — whether from deliberate annotation or promoted from live feedback — feed back into the bank and compound the flywheel.

### Label sources and confidence

Labels enter the system through three channels, with different epistemic weight:

**Deliberate annotation** (`labeled_by: scientist_retro`) — the Scientist reviews hunches in the annotation UI with full conversation context, inspects artifacts, and renders a considered tp/fp/skip judgment. This is the highest-confidence label source. One `labels.jsonl` per eval run (in the run's output directory).

**Live operational feedback** (`labeled_by: operational_live`) — generated by `hunch run` when the Scientist presses g/b/s on a hunch in the side panel. This is an operational reaction gating injection, not a deliberate evaluation. A "good" means "pass this to the Researcher, it looks reasonable" — the Scientist may not have fully investigated. A "skip" means "not now / can't tell" — explicitly a non-judgment. Live feedback lives in `feedback.jsonl` in the replay directory, separate from `labels.jsonl`.

Live feedback is the most ecologically valid signal — it captures the Scientist's in-the-moment reaction with full session context. But it is not gold-standard ground truth: the bar for pressing "good" is lower than the bar for labeling "tp." The flywheel bridge promotes live feedback to label-bank entries as candidates: good → candidate tp, bad → candidate fp, skip → unlabeled. The `labeled_by` field preserves provenance so downstream consumers can weight accordingly.

**Anchor / mined labels** (`labeled_by: anchor`, `mined`) — hand-curated known-good catches or automated mining of the transcript for moments where the Scientist themselves raised a concern. These are just additional bank entries that participate in auto-matching.

When live feedback and retrospective annotation conflict on the same concern, the live label has higher ecological validity (the Scientist was there) but the retrospective label has higher deliberative confidence (the Scientist investigated). In practice this tension rarely arises — when it does, the annotation UI surfaces the conflict for the Scientist to resolve.

## The annotation UI

```
hunch annotate-web --run-dir <dir>
```

Starts a local server and opens a browser-based two-pane UI:

![Annotation UI](img/annotate_web.png)

- **Left pane:** Hunch details — smell, description, and referenced artifact content reconstructed from the replay buffer's artifact state at that bookmark.
- **Right pane:** Conversation context — dialogue from the replay buffer, centered on the hunch's triggering window (`bookmark_prev..bookmark_now`). Both panes scroll independently.

**Navigation:** Previous/next buttons (or keyboard shortcuts) to move between hunches. Already-labeled hunches show their label but can be re-labeled.

**Labeling:** TP / FP / Skip buttons. Writes immediately to `labels.jsonl`. Optional fields for category tag and free-text note.

**Artifact reconstruction:** For each hunch, the UI reconstructs the state of referenced `.md` artifacts at `bookmark_now` by replaying `artifact_write` and `artifact_edit` events from the replay buffer up to that `tick_seq`.

## The shareable report

```
hunch eval report --run-dir <dir>
```

Produces `eval_report.json` + prints a human-readable summary.

### eval_report.json

```json
{
  "critic_version": "sonnet-v1",
  "run_date": "2026-04-16",
  "project": "agentic_research",
  "ticks_fired": 12,
  "total_hunches": 9,
  "novelty": {
    "novel": 6,
    "already_raised": 3
  },
  "labels": {
    "tp": 5,
    "fp": 1,
    "skip": 0,
    "unlabeled": 3,
    "auto_matched_from_bank": 2
  },
  "precision": 0.83,
  "categories": {
    "confound": 2,
    "measurement": 2,
    "contradiction": 1
  },
}
```

**What's NOT in the report:** hunch descriptions, conversation excerpts, artifact content, notes. Only counts and rates.

### Human-readable summary (printed to terminal)

```
Critic sonnet-v1 on agentic_research (12 ticks)
  Hunches: 9 (6 novel, 3 already-raised)
  Labeled: 6/9 — 5 tp, 1 fp (precision 83%)
    └─ 2 auto-matched from label bank, 4 Scientist-labeled
  Categories: confound (2), measurement (2), contradiction (1)
```

This is what the Scientist pastes in Slack.

## Cross-run comparison (future)

```
hunch eval compare run03 run04
```

Side-by-side: did precision go up? New catches? Regressions? Deferred until we have multiple labeled runs to compare.

## Implementation plan

### Phase 1: Labels + report (minimal, no UI, no bank)
1. `labels.jsonl` read/write utilities
2. `hunch eval label <run-dir> <hunch-id> <tp|fp|skip>` — CLI labeling
3. `hunch eval report <run-dir>` — generates eval_report.json + prints summary
4. Wire up novelty judge

### Phase 2: Label bank + auto-matching
1. `label_bank.jsonl` read/write utilities (project-level, beside replay file)
2. Semantic matcher: compare new hunches against bank entries
3. `hunch eval automatch <run-dir>` — runs novelty filter → bank match → writes auto-labeled entries to labels.jsonl with `source: bank`
4. Round-trip: when Scientist labels a new hunch (Phase 1 CLI), also append to bank

### Phase 3: Annotation UI
1. Artifact snapshot reconstruction (from replay buffer)
2. Dialogue context renderer (from replay buffer, centered on hunch's triggering window)
3. Browser-based UI: two-pane layout, navigation, labeling hotkeys
4. Surface only unlabeled hunches by default; option to review auto-matches
5. `hunch annotate-web --run-dir <dir>` (starts local server)

### Phase 4: Polish
1. Cross-run comparison
2. Category suggestions (LLM-assisted)
3. Resolution tracking (was the concern addressed in conversation?)
4. Bank hygiene: flag stale entries, surface disagreements between Scientists
