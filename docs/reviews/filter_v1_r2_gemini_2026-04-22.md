# Filter v1 Round 2 — Gemini 2.5 Pro Adversarial Review (2026-04-22)

Model: gemini-2.5-pro (hit 429 capacity errors mid-review but completed)

Context: Second-round review after fixing findings from round 1 (offline pipeline wiring,
SDK client consistency, parallel dedup, integration test).

## Findings

1.  **MAJOR: `_cmd_replay_offline` creates `HunchFilter` but never calls `init_from_existing`.**
    The online pipeline (`Runner.__post_init__`) seeds the filter's dedup window from existing
    hunches on disk via `init_from_existing(read_current_hunches(...))`. The offline pipeline
    creates the filter but skips this step, so the dedup window starts empty. Cross-tick dedup
    within a single run still works (hunches are added to `_prior_hunches` in `filter_batch`),
    but resuming from a partial run would miss prior hunches.

    **Status: FIXED** — added `init_from_existing` call in `_cmd_replay_offline`.

2.  **MINOR: Dead field `_PriorHunch.hunch_id`.**
    The `hunch_id` field is set in both `init_from_existing` (from `HunchRecord.hunch_id`) and
    `filter_batch` (hardcoded to `""`), but never read by the dedup logic — only `smell` and
    `description` are used in prompt rendering.

    **Status: FIXED** — removed `hunch_id` from `_PriorHunch`.

3.  **MINOR: No test for cross-tick deduplication in replay driver.**
    The integration test (`test_replay_from_dir_applies_filter`) only runs a single tick.
    A multi-tick test would verify that hunches emitted in tick 1 are properly deduplicated
    against in tick 2 — the exact scenario that the `init_from_existing` bug would break.

    **Status: FIXED** — added `test_cross_tick_dedup_in_replay`.

4.  **NIT: `ThreadPoolExecutor.cancel()` on futures doesn't stop already-running threads.**
    In `_check_dedup`, when a duplicate is found, `future.cancel()` is called on remaining
    futures. This only prevents pending (not-yet-started) futures from running; already-running
    threads continue to completion. For a window of ≤10 fast Haiku calls this is harmless.

5.  **No gross code replication found.** Both pipelines share `HunchFilter` and the filter
    prompt templates. The persist logic in `driver.py:_persist_hunches` and `run.py:_fire_tick`
    are structurally similar but not identical (driver uses a helper, runner inlines), which is
    acceptable given their different contexts.
