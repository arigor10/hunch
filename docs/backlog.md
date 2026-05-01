# Backlog — Known Issues & Deferred Work

Items that came up during development but aren't blocking current work. Grouped by area, roughly priority-ordered within each group.

## Critic / Model Calls

- **No prompt caching in offline eval.** The Sonnet critic calls `claude --print` per tick — each call is independent, no session reuse, no cache hits. For a 200-tick eval this means paying full input price on every call. Fix: use the SDK client with prompt caching (stable system prompt prefix gets cached across calls within a 5-minute window).

- **Log line `proj_tokens == input_tokens` is uninformative.** `projected_tokens()` is called immediately after `update_observed_tokens()`, so it always equals the observation. Should either log `cache_read_input_tokens` separately (useful for diagnosing cache hit rates) or log the projection *before* the call (useful for diagnosing estimation accuracy).

## Replay Buffer

- **Stale replay dir has no catch-up mechanism.** If `hunch run` was stopped and restarted, the replay dir is up to date (checkpoint-based resume). But if the replay dir was built by an earlier `--claude-log` parse and the transcript has grown since, there's no way to incrementally update it — you'd have to delete and re-parse. A future `hunch refresh-replay` or automatic top-up in `--claude-log` mode would help.

## Checkpoint / Resume

- **Race condition between hunch write and checkpoint write.** In both online and offline modes, hunches are written to disk before the checkpoint is updated. A crash in that window produces duplicate hunches on resume. Mitigated by the dedup filter, but the persistence layer shouldn't rely on application-level dedup for correctness. Accepted for now; fix would require writing hunches and checkpoint atomically (e.g. WAL or combined file).

## Eval Infrastructure

- **~~Label bank not yet implemented.~~** ✓ Implemented. The label bank lives at `.hunch/bank/hunch_bank.jsonl` (event-sourced). `hunch bank sync` ingests eval runs, dedup-matches hunches across runs via LLM judge, and optionally migrates legacy `labels.jsonl`. See [`hunch_bank_design.md`](hunch_bank_design.md) for the full design.

- **`hunch eval report` not yet implemented.** The shareable report (precision/recall/category breakdown without raw content) is designed but not built.

## CLI / UX

- **`input_tokens` calculation sums all three usage fields.** In `_call_model`, `input_tokens = usage.input_tokens + cache_read + cache_create`. The Anthropic API's `input_tokens` field already excludes cached tokens, so this sum is the total prompt size (correct for token bookkeeping) but confusing if read as "tokens billed at full price." Not a bug, but the naming is misleading.
