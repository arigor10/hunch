# Critic v0.1 — accumulate + purge with inline state

*Status: design notes. Supersedes v0's windowed-snapshot scheme.
Written 2026-04-15 as the starting point for the implementation.*

## Why v0.1

v0 fed the Critic a moving window of recent chunks (last N chunks or
last N tokens). Two problems:

1. **Strictly dominated by cache economics.** Windowing shifts the
   prompt prefix every tick, so every call is a cache miss — Sonnet's
   prompt-cache TTL is ~5 min at ~10% the cost, and windowing throws
   that away. Accumulating the prompt and only paying full price for
   the new delta is cheaper *and* gives the Critic more context.
2. **Structurally blind to long-horizon noses.** Nose types that
   require remembering a commitment from chunk 12 while evaluating a
   claim in chunk 60 (silent model invalidation, direction-of-effect
   repeat offense) are unreachable through a 5-chunk window. Our first
   real-world runs on soft-prompting showed Critic hits leaning on
   artifact-only evidence — exactly what a short-horizon Critic can
   still do — while longer-arc noses are silent.

## Core architecture: accumulate + purge

One canonical prompt stream that **only grows by appending** during
normal operation. Nothing gets rewritten mid-session. This preserves
the prefix identity Sonnet's cache requires.

The stream has four regions, in order:

1. **Static preamble.** System prompt + Critic instructions. Fixed for
   the whole session.
2. **Surviving open hunches** *(rebuilt only at purge).* Hunches
   emitted before the purge boundary that are still open (unlabeled).
3. **Living artifacts snapshot** *(rebuilt only at purge).* Current
   content of `.md` artifacts that existed at the purge boundary, so
   later inline edits in the surviving timeline have a base to apply
   to.
4. **Timeline.** Pure append stream: chunk events, inlined hunch
   emissions, inlined feedback labels, artifact writes and edits. Grows
   indefinitely between purges.

## Purge policy

Low watermark 150k tokens, high watermark 200k (Sonnet's context
ceiling). When about to send a tick whose projected total exceeds the
high watermark, we *purge* — drop chunks from the start of the timeline
until cumulative token count is back to 150k. This triggers exactly
one cache miss: the post-purge prefix is fully rebuilt, then
accumulation resumes with 50k headroom before the next purge.

At purge, regions 2 and 3 are regenerated:

- **Open hunches** — walk `hunches.jsonl` + `feedback.jsonl`, keep
  hunches whose label is still missing. They move from their original
  inline position into the surviving-hunches block.
- **Living artifacts** — for every `.md` that exists at the purge
  boundary, snapshot its current content. Any write/edit events in the
  dropped chunks contributed to that current content but won't be
  re-shown individually; only the resulting snapshot survives.

### Artifact budget (LRU eviction)

In a long session the `.md` corpus grows without bound — a first pass
on the real AR replay ended up with 106 `.md` files totalling ~774k
chars, which alone exceeds Sonnet's context window. So region 3 has a
budget knob (`artifact_budget_tokens`, default ≈ `low_watermark / 2`).
When the current corpus exceeds it, we drop files starting from the
least-recently-touched and work forward until the snapshot fits.
Files touched by a write or edit (including an edit in the surviving
timeline after a purge) are considered fresh. The timeline still
carries the per-event writes and edits that happened since the last
purge, so even evicted files may show up in the timeline block; the
Critic just doesn't get their full baseline content in region 3.

## Token bookkeeping (no blind estimation)

Each response from the CLI (with `--output-format json`) includes
`usage.input_tokens`. We track this as ground truth for the prefix
size going into each tick. Only the *next* increment — the chunk we're
about to append plus any optional inline hunch/label events — needs an
estimate, which we do by character count with a generous margin.
This means:

- We always know `t_prefix` exactly.
- Purge trigger is deterministic against real numbers, not a guess at
  the whole prompt size.
- The purge *target* (150k) is also deterministic: walk chunk
  boundaries backward, summing bookkept contributions, until cumulative
  drops to ~150k.

## Inline hunch events

When the Critic emits a hunch at tick T, the full record — smell,
description, triggering_refs — is appended to the timeline at T. It is
never rewritten; subsequent ticks just see it there in the stream.
Benefits:

- **Pure append.** Cache holds.
- **Temporally grounded.** The LLM reads the hunch right after the
  evidence that triggered it, in the same narrative order.
- **Echo suppression falls out.** The Critic reading chunk T+5
  naturally sees its own earlier hunch inline; it can recognize and
  avoid re-raising the same concern without any special "previously
  raised hunches" scaffolding.

## Contemporaneous feedback labels

When the Scientist labels a hunch (good / bad / skip), the label is
appended to the timeline at the moment it was received — not to a tail
epilogue. Same append-only discipline as hunches and chunks:
`feedback_label` is just another event type in the stream. The Critic
reads "h-0007 fired at T, labeled bad at T+12" as a natural temporal
sequence, and prior labels inform its future emissions without any
separate index.

## Artifacts: inline events, snapshot only at purge

During normal accumulation, artifact writes and edits are inlined as
events at the time they occurred — full content for writes, the
`old_string` / `new_string` diff for edits. Sonnet is good at parsing a
chronological mutation history to infer current state, and this is
strictly cheaper than re-snapshotting whole files on every change.

The **only** exception is at purge: the surviving timeline may contain
edits to files whose initial `write` events got dropped, and without
base content those edits are ungrounded. So purge rebuilds the
"living artifacts" block (region 3 above) with current content of each
`.md` that exists at the purge moment. This reconstruction logic is
the single nontrivial piece of the engine and gets unit tests.

## Model access path

The Critic shells out to `claude --print --model <M> --output-format
json -p <prompt>` (as of this version). This uses the user's Claude
Code session (OAuth, subscription-billed) instead of requiring
`ANTHROPIC_API_KEY`. The SDK path remains available by injecting a
`client=` at construction time. The JSON output format is used so we
can parse usage metadata per call.

## Implementation surface

Roughly:

- `hunch/critic/accumulator.py` — new — maintains the prompt stream,
  token bookkeeping, purge trigger, post-purge rebuild. Unit-tested
  especially around snapshot reconstruction.
- `hunch/critic/sonnet.py` — consumes the accumulator's rendered
  prompt, shells out to `claude --print`, and feeds observed
  `usage.input_tokens` back into the bookkeeper.
- `hunch/critic/prompts/nose_v1.md` — adapted prompt template aware of
  inline hunches + labels + living-artifacts block.
- Tests for: the four-region render; purge math; snapshot
  reconstruction across write/edit chains; open-hunch survival; token
  bookkeeping round-trip.

## Open calls to make during build

- **Inline hunch record exact format** — full JSON vs a readable
  rendered block. Leaning rendered-block (prose preamble + indented
  JSON) for LLM readability.
- **Purge-time "living artifacts" definition** — all `.md` files
  referenced in the surviving timeline? All `.md`s that existed? First
  pass: all existing `.md`s at purge moment; trim later if bloat.
- **Mini-judge for echo suppression** — separate follow-up; inline
  hunches make this simpler but not automatic.
