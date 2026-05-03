# Critic v1: Wiki Critic

## What this is

The next version of the Hunch critic. Instead of a single LLM call per tick
with an accumulator-managed sliding window, each tick is an **agentic
`claude -p` invocation** that maintains a persistent **wiki** — a structured
knowledge base of what the research has established so far.

The wiki replaces the accumulator. Long-term context lives in the wiki
(claims, concepts, hypotheses, open questions). Short-term context is the
latest conversation block. The critic reads both, updates the wiki, and
emits hunches — all within a single agentic turn.

## Why

In v0, the critic sees a window of raw transcript and must do everything
in a single pass: understand the research context, remember what's been
established, and spot anomalies — all from raw data. We can guide it
with a good prompt, but the model's understanding is implicit and
ephemeral. It resets every tick. There's no persistent structure we can
inspect, correct, or build on.

The wiki critic is a different paradigm. Instead of relying solely on
the model's native pattern-matching, we give it **external scaffolding
for knowledge management** — a persistent, structured artifact where it
maintains its understanding of the research. The model already knows how
to reason about science; what it can't do natively is maintain a
coherent picture of a project across hundreds of ticks. The wiki
provides that, in much the same way a lab notebook augments a
scientist's native reasoning: not by making them smarter, but by
externalizing their understanding so it persists, accumulates, and
remains inspectable.

This gives us two things v0 cannot offer:

1. **A lever to augment the model's native capabilities.** We define the
   scaffolding — which entity types to track, how to propagate changes,
   when to prioritize learning vs critiquing — and the model fills it
   with its reasoning. The methodology lives in the CLAUDE.md schema,
   iterable separately from the prompt. When the critic misses
   something, we can ask: was the reasoning wrong, or was the
   scaffolding missing a structure that would have surfaced it?

2. **Structured context instead of raw tokens.** The wiki retains what
   matters semantically (claims, hypotheses, evidence) and discards
   what doesn't (routine back-and-forth). This also solves the context
   window problem — v0's sliding window drops old content regardless
   of importance — but that's a side benefit, not the core motivation.

### Known risks

**Epistemic drift.** The wiki is LLM-authored. Over many ticks, the critic's
understanding can diverge from ground truth — the wiki becomes a closed
loop citing its own summaries. Mitigations:

- Every wiki entity must link to raw evidence (`tick_seq` numbers, artifact
  paths). The critic can always drill down to check its understanding.
- Provenance tracking: every claim records HOW the wiki knows it
  (EXTRACTED from transcript, INFERRED by critic, HUMAN-VERIFIED).
- Conservative status transitions: the CLAUDE.md schema instructs the
  critic to prefer gaps over wrong entries.
- Eval: compare wiki-critic hunches against v0 (accumulator) hunches on
  the same transcript. If the wiki degrades hunch quality, we'll see it.

**Change propagation cascades.** An artifact edit can make evidence stale,
which affects claims, which might affect other claims that cite those claims.
Deep cascades are expensive and error-prone. Mitigations:

- Lazy staleness: mark stale immediately, re-evaluate incrementally.
- Depth limits: the CLAUDE.md can instruct the critic to propagate one
  level per tick. Transitive effects are caught on subsequent ticks.
  The wiki trends toward consistency rather than achieving it atomically.
- The CLAUDE.md can enforce prioritization — e.g., learning before
  maintenance before critiquing — so the critic always absorbs new
  knowledge first and never critiques on stale foundations. See the
  [example CLAUDE.md](example_critic_v1_claude_md.md) for one approach.

**Bootstrapping.** Early ticks have an empty wiki and short conversation
history. The critic has little context. Mitigations:

- Seed pass: before tick 1, run an agent pass over project documentation
  (README, design docs, CLAUDE.md) to extract initial concepts and
  questions.
- The CLAUDE.md can prioritize extraction (building the wiki) over
  critique (raising hunches) when the wiki is sparse.

## Architecture

The workspace shape is identical in both modes. Only the data source
and location differ.

```
 OFFLINE EVAL                          LIVE
 ────────────                          ────
 pre-parsed transcript                 real-time Claude transcript
       │                                     │
  replay driver                         capture writer
  (builds incrementally)               (appends in real time)
       │                                     │
       ▼                                     ▼
  .hunch/eval/<run>/                    .hunch/replay/
       │                                     │
       └──────────┐          ┌───────────────┘
                  ▼          ▼
         ┌──────────────────────────────────┐
         │          workspace               │
         │                                  │
         │  conversation.jsonl (replay buf) │
         │  artifacts.jsonl                 │
         │  artifacts/                      │
         │  wiki/              (persistent) │
         │  wiki_contract_spec.md (format)  │
         │  wiki_contract.yaml (schema chk) │
         │  current_block.md   (latest conv)│
         │  project_docs/      (seed)       │
         │  CLAUDE.md          (schema)     │
         │  .claude/settings.json (sandbox) │
         │  pending_hunches.jsonl  (agent→) │
         │  hunches.jsonl      (→driver)    │
         └──────────────┬───────────────────┘
                        │
              claude -p │ (one invocation per tick)
                        │
         ┌──────────────▼──────────────────────┐
         │       agent (agentic turn)          │
         │                                     │
         │  1. Orient (CLAUDE.md + index.md)   │
         │  2. Read current_block.md           │
         │  3. Learn (extract to wiki)         │
         │  4. Maintain (consistency checks)   │
         │  5. Critique (raise concerns)       │
         │  6. Update wiki/index.md            │
         │  7. Write pending_hunches.jsonl     │
         └─────────────────────────────────────┘
```

### Components

1. **CLAUDE.md** — the operating schema. Defines entity types, frontmatter
   fields, edge vocabulary, ingest workflow, hunch output format, and the
   learning-vs-critiquing balance. This is where most iteration happens.
   Changing the schema requires zero code changes.

2. **Workspace renderer** — Python code that builds/updates the eval run
   directory for each tick:
   - Renders `current_block.md` from events since last tick
   - Incrementally appends events to `conversation.jsonl` and
     `artifacts.jsonl` (causal by construction — future events haven't
     been written yet)
   - Writes `.claude/settings.json` sandbox (once, at init)
   - Manages `project_docs/` (static, copied at init)

3. **Driver** — thin outer loop:
   - Reads events from replay buffer (same as v0)
   - Fires trigger (same TriggerV1 policy)
   - Calls workspace renderer
   - Invokes `claude -p` in the workspace
   - Collects hunches from `pending_hunches.jsonl`
   - Wraps hunches with metadata (tick_id, bookmarks, timestamps)
   - Writes to output `hunches.jsonl` in standard hunch format
   - Optionally: git-commits the workspace after each tick

4. **Permission sandbox** — `.claude/settings.json` that restricts
   Read/Edit/Write to paths within the workspace. The agent cannot
   access the full replay log or other filesystem paths.

### Design principle: CLAUDE.md as the swappable brain

The aspiration is that **all the intelligence lives in CLAUDE.md**, and
the surrounding code (driver, renderer, permissions, linter) is generic
infrastructure that doesn't change when the schema changes. A scientist
who wants to try a different approach — different entity types, different
propagation rules, different learning/critiquing balance — swaps the
CLAUDE.md file and re-runs. The driver doesn't care.

This means the driver must have **zero knowledge of entity types,
frontmatter fields, or edge names.** It knows three things:

1. Where to write `current_block.md` (convention)
2. Where to read hunches from (convention: `pending_hunches.jsonl`)
3. The hunch output format (smell + description + triggering_refs)

Everything else — what the wiki contains, how the agent navigates it,
when to learn vs critique — is between the CLAUDE.md and the agent.

**Linting the wiki.** The agent will make mistakes: dangling references,
malformed frontmatter, asymmetric edges. Over hundreds of ticks, even a
low per-tick error rate compounds into a corrupted wiki. We need
validation, but it must not break the swappability of CLAUDE.md.

Three layers:

1. **`wiki_contract.yaml`** — a machine-checkable schema generated by the
   agent on its first tick, derived from the CLAUDE.md it was given. The
   contract declares entity types, required frontmatter fields, allowed
   enum values, and bidirectional edge pairs. The example below matches
   our [first CLAUDE.md](example_critic_v1_claude_md.md) — a different
   CLAUDE.md would produce a different contract:

   ```yaml
   entity_types:
     concept:
       required_fields: [id, type, created]
     claim:
       required_fields: [id, type, created, status, confidence, provenance]
       status_values: [conjectured, supported, well-supported, contested, refuted, obsolete]
     evidence:
       required_fields: [id, type, created, source-type, status, source-artifact]
       status_values: [current, stale, invalidated]
     hypothesis:
       required_fields: [id, type, created, status]
       status_values: [open, testing, confirmed, falsified, abandoned]
     question:
       required_fields: [id, type, created, status]
       status_values: [open, dormant, answered]
   bidirectional_edges:
     - [supersedes, superseded-by]
     - [supports, supported-by]
     - [supports, evidence-for]
     - [refutes, refuted-by]
     - [refutes, evidence-against]
   ```

   The agent generates this, not us. When someone swaps the CLAUDE.md to
   use `theorem` and `proof` entity types, the agent generates a new
   contract. The framework never hardcodes entity names.

2. **Meta-validator** (Python, runs once after the contract is generated):
   checks that the contract itself is well-formed — has `entity_types`
   with `required_fields`, enum lists are non-empty where declared,
   bidirectional edge pairs are symmetric. This is the only thing we
   validate "about" the CLAUDE.md, and we do it via the contract, not by
   parsing the CLAUDE.md directly.

3. **Per-tick validator** (Python, runs after every tick): reads the
   contract and checks every wiki file against it:
   - Entity type in frontmatter matches a contract type
   - All required fields present and non-empty
   - Enum fields (`status`, `provenance`, etc.) have allowed values
   - Bidirectional edges: if A lists `supports: [B]`, B must list
     `supported-by: [A]`
   - IDs unique across the wiki
   - No dangling references (every ID mentioned in an edge exists)
   - ID format matches `<type>-<slug>` convention

   This is generic code written once. It reads `wiki_contract.yaml` at
   runtime and validates against whatever schema the contract declares.
   No code changes when the CLAUDE.md changes.

4. **Semantic audit** (LLM-based, runs periodically): a separate
   `claude -p` invocation that reads the CLAUDE.md and checks the wiki
   for higher-level issues the contract can't express — stale narrative
   in `index.md`, claims whose evidence doesn't actually support them,
   entity descriptions that have drifted from their source material.
   The Karpathy "nightly lint" pattern.

**On validation failure:** The per-tick validator runs after each tick
and reports violations. The driver logs them. If violations exceed a
threshold (configurable, e.g. 5 errors), the driver can inject a
repair instruction into the next tick's prompt: "The following wiki
violations were found after your last tick: [list]. Fix them before
proceeding." This keeps the agent self-correcting without human
intervention.

**Why generate the contract, not write it by hand?** The contract
duplicates information already in the CLAUDE.md. Writing it manually
creates a sync burden — change the schema in CLAUDE.md, forget to
update the contract, and the validator rejects valid entities. Having
the agent generate the contract from the CLAUDE.md keeps them
aligned by construction. The meta-validator catches malformed
contracts, and the first few ticks quickly reveal if the contract
doesn't match the CLAUDE.md's actual instructions (the agent's own
edits will fail validation).

**CLAUDE.md instructions for contract generation:**

The contract format is defined in `wiki_contract_spec.md` — a
framework-provided reference doc copied into the workspace at init
(alongside CLAUDE.md). Any CLAUDE.md just needs to include:

```
On your first tick, before processing any conversation:
1. Read this entire file (CLAUDE.md)
2. Read wiki_contract_spec.md for the contract format
3. Generate wiki_contract.yaml declaring all entity types, required
   fields, allowed enum values, and bidirectional edge pairs
4. The framework will validate the contract and your wiki edits
   against it on every subsequent tick
```

Scientists writing custom CLAUDE.md schemas don't need to know the
contract format — they just point to `wiki_contract_spec.md` and the
agent figures out the rest.

### Workspace layout

The workspace mirrors `.hunch/replay/` — replay buffer files live at the
root level, wiki sits alongside them. This unifies live and eval modes
under one shape.

```
<workspace>/
├── CLAUDE.md                        # operating schema (swappable)
├── wiki_contract_spec.md            # contract format reference (framework-provided)
├── wiki_contract.yaml               # agent-generated, machine-checkable schema
├── .claude/
│   └── settings.json                # permission sandbox
├── conversation.jsonl               # replay buffer (root level)
├── artifacts.jsonl                  # artifact events
├── artifacts/                       # artifact snapshots
├── wiki/                            # grows with each tick (the new thing)
│   ├── index.md                     #   entry point (required)
│   └── <subdirs per CLAUDE.md>      #   entity files, structure varies
├── current_block.md                 # overwritten each tick by driver
├── project_docs/                    # static seed material (copied at init)
├── pending_hunches.jsonl            # agent writes here; driver reads + clears
├── hunches.jsonl                    # standard hunch output (driver writes)
└── checkpoint.json                  # for resume
```

**Eval mode:** `<workspace>` = `.hunch/eval/<run_name>/`. The driver
incrementally builds `conversation.jsonl`, `artifacts.jsonl`, and
`artifacts/` inside the workspace — same files that `ReplayBufferWriter`
creates, built tick-by-tick. Future events don't exist yet, so causal
isolation is by construction. The wiki is a first-class run output
alongside `hunches.jsonl` — two runs with different CLAUDE.md schemas
produce different wikis, diffable and auditable.

**Live mode** (Phase 5): `<workspace>` = `.hunch/replay/`. The live
capture writer already populates `conversation.jsonl` and `artifacts/`
here. We add `wiki/`, `CLAUDE.md`, and `current_block.md` alongside.
The permission sandbox restricts the agent to `.hunch/replay/`, so it
can't wander to `.hunch/bank/`, `.hunch/eval/`, or other sibling dirs.
The wiki persists across sessions because `.hunch/replay/` persists.

### What's reused from existing code

| Existing code | Reused for |
|---|---|
| `hunch/replay/driver.py` | Replay loop structure, trigger integration, checkpoint/resume |
| `hunch/journal/hunches.py` | Hunch output format, `HunchesWriter` |
| `hunch/trigger.py` | TriggerV1 policy (unchanged) |

The driver needs a conversation renderer (for `current_block.md`) and an
agent invocation wrapper (`subprocess` + `claude -p` + `--allowedTools`).
These will be written as new modules in the hunch repo, informed by
prior prototypes.

## CLAUDE.md contract

The framework requires very little from a CLAUDE.md. Everything else —
entity types, edge vocabulary, tick strategy, change propagation rules —
is the CLAUDE.md's internal business.

**Required conventions** (the framework depends on these):

1. **Contract generation.** On the first tick, generate
   `wiki_contract.yaml` per `wiki_contract_spec.md`. The per-tick
   validator uses this to catch structural errors.

2. **Hunch output.** Write hunches to `pending_hunches.jsonl` as JSON
   lines with at least `smell`, `description`, and `triggering_refs`
   (`tick_seq` numbers and artifact paths the Scientist can look up). Hunches
   cite the observable conversation and artifacts, not wiki internals.

3. **Wiki directory.** Store entities as markdown files with YAML
   frontmatter under `wiki/`. Subdirectory structure is up to the
   CLAUDE.md.

4. **Index.** Maintain `wiki/index.md` as the agent's entry point —
   read first on each tick to orient.

**Everything else is flexible.** Entity types, field names, edge
vocabulary, status values, propagation strategy, tick prioritization —
all defined by the CLAUDE.md and validated via the contract it generates.

Our first CLAUDE.md is documented at
[`example_critic_v1_claude_md.md`](example_critic_v1_claude_md.md). It
defines five entity types (Concept, Claim, Evidence, Hypothesis,
Question), a lazy staleness model for change propagation, and three
tick phases (learn → maintain → critique). This is the schema
we'll use for our first eval runs — it will evolve. The infrastructure
won't need to change when it does.

## The tick loop in detail

### Workspace initialization (once per run)

```
1. Create workspace directory
2. Copy CLAUDE.md into workspace
3. Write .claude/settings.json (permission sandbox)
4. Create wiki/ with empty index.md
5. Copy project docs into project_docs/
6. Run claude -p "Generate wiki_contract.yaml per CLAUDE.md"
7. Run meta-validator on wiki_contract.yaml
   → fail fast if the contract is malformed
8. If seed pass enabled:
   a. Run claude -p "Seed the wiki from project_docs/ per CLAUDE.md"
   b. Run per-tick validator on wiki/
   c. Git commit: "seed: initial wiki from project docs"
```

### Per-tick loop

```
1. Process events through ReplayBufferWriter into the eval run dir
   (same as v0 — appends to conversation.jsonl, artifacts.jsonl,
   writes artifact snapshots to artifacts/)
2. Check trigger (same TriggerV1 policy)
3. If trigger fires:
   a. Render current_block.md from events since last tick
      - Conversation text (user/assistant turns)
      - Artifact writes/edits (with content or diffs)
      - Tool errors
   b. Invoke claude -p in the eval run dir:
      claude -p --bare \
        --allowedTools "Read,Edit,Write,Grep,Glob" \
        --output-format json \
        "A new conversation block has arrived at current_block.md. \
         Process it per CLAUDE.md."
   e. Run per-tick validator on wiki/ against wiki_contract.yaml
      - Log violations; if count > threshold, inject repair prompt on next tick
   f. Collect hunches from pending_hunches.jsonl
   g. Wrap with metadata (tick_id, bookmarks, timestamps)
   h. Write to output hunches.jsonl (standard hunch format)
   i. Clear pending_hunches.jsonl
   j. Git commit: "tick {N}: {summary}" (optional)
4. Advance cursor
```

### Current block rendering

The driver renders `current_block.md` from replay buffer events. Format:

```markdown
# Conversation Block (tick 42)

Replay turns 380-415 | 2026-03-15 14:22 – 14:58

---

**ARIEL:** Let's try steering at layer 14 with the rotation vector...

**CLAUDE:** I'll set up the experiment. [runs code]

[ARTIFACT WRITE: results/exp026/layer14_rotation.md]
[Content (1,200 chars):]
...artifact content...

**CLAUDE:** The results show rotation at 311° with...

[ARTIFACT EDIT: docs/results_registry.md]
[Changed: '...' -> '...']

**ARIEL:** Interesting, that matches the Gemma sweep...
```

This works with the hunch replay buffer event format (`conversation.jsonl`
events).

### Hunch output

The agent writes raw hunches to `pending_hunches.jsonl`. Hunches can
arise from any tick phase — critiquing (the primary case), maintenance
(contradictions found during re-evaluation), or learning (questionable
claims spotted during extraction). Hunches cite the observable
conversation and artifacts — what the Scientist can see — not wiki
internals:

```json
{
  "smell": "Llama rotation contradicts 311° invariance claim",
  "description": "Experiment in exp026/llama_rotation.md shows rotation at 280°, but the layer-14 results in exp026/layer14_rotation.md established 311° ± 3° (tick_seq 385). The 30° discrepancy is well outside the error bars.",
  "triggering_refs": {
    "tick_seqs": [385, 412],
    "artifacts": ["results/exp026/layer14_rotation.md", "results/exp026/llama_rotation.md"]
  }
}
```

The driver wraps with metadata to produce standard hunch format:

```json
{
  "type": "emit",
  "tick_id": "tick-042",
  "hunch_id": "h-0012",
  "smell": "...",
  "description": "...",
  "triggering_refs": {"tick_seqs": [...], "artifacts": [...]},
  "bookmark_prev": 380,
  "bookmark_now": 415,
  "timestamp": "2026-03-15T14:58:00Z",
  "critic_version": "wiki-v1"
}
```

## Permission sandbox

`.claude/settings.json` in the workspace:

```json
{
  "permissions": {
    "deny": ["Read", "Edit", "Write", "Bash"],
    "allow": [
      "Read({{workspace}}/**)",
      "Edit({{workspace}}/**)",
      "Write({{workspace}}/**)",
      "Grep({{workspace}}/**)",
      "Glob({{workspace}}/**)"
    ]
  }
}
```

`{{workspace}}` is replaced with the absolute workspace path at init.
Bash is denied entirely — the agent navigates via Read/Write/Grep/Glob
only. This prevents the agent from accessing the full replay log or
other filesystem paths.

## Seeding

Before tick 1, the driver optionally runs a **seed pass**: a separate
`claude -p` invocation that reads `project_docs/` and populates the wiki
with initial concepts and questions.

What goes in `project_docs/`:
- The target project's README.md
- The target project's CLAUDE.md (if it has one)
- Key design docs (manually selected or auto-discovered)
- Optionally: a project summary written by the user

The CLAUDE.md should include seed instructions — what to extract from
project docs and how to populate the wiki from it. See the
[example CLAUDE.md](example_critic_v1_claude_md.md) for a concrete
version. The key constraint: do NOT raise hunches during seeding —
there's not enough context yet.

## Eval integration

The wiki critic produces hunches in the standard hunch format, so
existing eval infrastructure works:

- `hunch filter` — dedup + novelty filtering
- `hunch bank sync` — ingest into label bank
- `hunch annotate-web` — labeling UI
- Comparison runs: same transcript, wiki-critic vs v0, compare recall

New eval dimensions specific to the wiki critic:

- **Wiki quality audit**: after a run, check the wiki for internal
  consistency (dangling refs, status contradictions, orphan entities).
  This can be a separate `claude -p` invocation over the final wiki
  state.
- **Wiki drift detection**: compare wiki claims against ground truth
  (human-verified claims about the research). Measures epistemic drift.
- **Cost per tick**: `--output-format json` gives cost/tokens per
  invocation. Track and compare with v0.
- **`triggering_refs` coverage**: do hunches that cite more specific
  `tick_seq` numbers and artifact paths have higher precision? This
  measures whether grounded references correlate with hunch quality.

## Cost estimate

Each tick is a full `claude -p` invocation with ~8-15 tool calls:
- Read CLAUDE.md, index.md, current_block.md (3 reads)
- Grep/read relevant wiki entities (2-5 reads)
- Write/edit wiki updates (1-4 writes)
- Write hunches (0-1 writes)

Rough per-tick cost (Sonnet 4.6):
- Input: ~20-40K tokens (CLAUDE.md + wiki context + current block)
- Output: ~2-5K tokens (wiki updates + hunches + tool calls)
- Estimated: $0.03-0.08 per tick

For a 200-tick AR replay: **$6-16 per run.**
Compare with v0 (single-call Sonnet): ~$2-4 per run with caching.

The wiki critic is 3-4x more expensive per run. Acceptable for offline
eval; for live use, consider using a cheaper model for routine ticks
and Sonnet only when the wiki state suggests something complex is
happening.

## Implementation plan

### Phase 1: Workspace renderer + driver skeleton

- Workspace init (create dirs, copy CLAUDE.md, write settings.json)
- Current block renderer (adapt from `render_chunk_dialogue`)
- Driver loop: events → trigger → render → invoke `claude -p` → collect hunches
- Hunch metadata wrapping
- No seeding, no git audit trail yet

Reuse: `driver.py` replay loop + trigger integration.

### Phase 2: CLAUDE.md v1

- Flesh out `example_critic_v1_claude_md.md` into a working CLAUDE.md
- Define tick workflow (orient → read → learn → maintain → critique → update index → hunches)
- Test manually: run one tick, inspect wiki output, iterate

### Phase 3: Seeding + first full run

- Implement seed pass (project docs → initial wiki)
- Run full AR replay with wiki critic
- Compare hunches against v0 baseline
- Iterate on CLAUDE.md based on hunch quality

### Phase 4: Hardening

- Git audit trail (commit after each tick)
- Wiki quality audit pass (separate claude -p invocation)
- Cost tracking and logging
- Checkpoint/resume for the wiki critic driver

### Phase 5: Live mode

- Wire into `hunch run` as an alternative to the sonnet critic
- Turn-end trigger feeds wiki critic instead of v0
- Wiki persists across sessions in `.hunch/wiki/`

## Open questions

1. **Model choice.** Sonnet 4.6 for all ticks, or Haiku for routine
   ticks + Sonnet for complex ones? The learning/critiquing balance
   might map to model choice: learning ticks (wiki extraction) could
   use Haiku, critiquing ticks (raising concerns) need Sonnet.

2. **Wiki size scaling.** At what point does the wiki outgrow what the
   agent can navigate in a single turn? For Ariel's research (months
   of work), probably dozens to low hundreds of entities — should be
   fine. For longer-running projects, may need wiki summarization or
   hierarchical indices.

3. **Multi-project wikis.** If the critic watches multiple projects
   (AR + SP), should they share a wiki? Separate wikis miss cross-
   project connections. Shared wikis risk cross-contamination. Probably
   separate wikis with an occasional cross-project audit.

4. **Artifact access in sandbox.** The agent needs to read artifact
   snapshots that are referenced from wiki entities. These are in
   `replay/artifacts/`. The sandbox allows this (it's inside the
   workspace), but the artifacts directory grows over a full replay.
   May need to limit to artifacts referenced by wiki entities.

5. **Concurrency with live sessions.** In live mode, the wiki critic
   would fire while the user is actively working. The wiki must handle
   concurrent reads (by the user, if browsing) and writes (by the
   critic). File-level locking or a write-ahead log? Probably fine
   with file-level operations at our tick frequency.
