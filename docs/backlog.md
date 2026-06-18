# Backlog — Known Issues & Deferred Work

Items that came up during development but aren't blocking current work. Grouped by area, roughly priority-ordered within each group.

## Critic / Model Calls

- **~~No prompt caching in offline eval.~~** ✓ Resolved. The backend unification (engine.py + pluggable backends) means offline eval can use `anthropic_sdk` backend with prompt caching, or any other backend via TOML config. See `configs/` for examples.

- **Log line `proj_tokens == input_tokens` is uninformative.** `projected_tokens()` is called immediately after `update_observed_tokens()`, so it always equals the observation. Should either log `cache_read_input_tokens` separately (useful for diagnosing cache hit rates) or log the projection *before* the call (useful for diagnosing estimation accuracy).

## Replay Buffer

- **Stale replay dir has no catch-up mechanism.** If `hunch run` was stopped and restarted, the replay dir is up to date (checkpoint-based resume). But if the replay dir was built by an earlier `--claude-log` parse and the transcript has grown since, there's no way to incrementally update it — you'd have to delete and re-parse. A future `hunch refresh-replay` or automatic top-up in `--claude-log` mode would help.

## Checkpoint / Resume

- **Race condition between hunch write and checkpoint write.** In both online and offline modes, hunches are written to disk before the checkpoint is updated. A crash in that window produces duplicate hunches on resume. Mitigated by the dedup filter, but the persistence layer shouldn't rely on application-level dedup for correctness. Accepted for now; fix would require writing hunches and checkpoint atomically (e.g. WAL or combined file).

## Eval Infrastructure

- **~~Label bank not yet implemented.~~** ✓ Implemented. The label bank lives at `.hunch/bank/hunch_bank.jsonl` (event-sourced). `hunch bank sync` ingests eval runs, dedup-matches hunches across runs via LLM judge, and optionally migrates legacy `labels.jsonl`. See [`hunch_bank_design.md`](hunch_bank_design.md) for the full design.

- **`hunch eval report` not yet implemented.** The shareable report (precision/recall/category breakdown without raw content) is designed but not built.

## Bank / Dedup Ordering

- **Mined hunches should be deduped before eval hunches.** Currently `hunch bank sync` processes runs in whatever order they're ingested. If eval runs enter the bank before mined runs, eval hunches become dedup anchors and mined hunches get matched against them — inverting the intended hierarchy. Mined hunches are ground truth (what the user actually noticed); they should form the canonical set that eval hunches are measured against. Correct ordering: (1) self-dedup within each mined set, (2) dedup mined sets against each other, (3) match eval hunches against the deduped mined corpus (and against each other). Also verify: are mined hunches currently self-deduped within a single mining run, or only across runs?

## CLI / UX

- **`input_tokens` calculation sums all three usage fields.** In `_call_model`, `input_tokens = usage.input_tokens + cache_read + cache_create`. The Anthropic API's `input_tokens` field already excludes cached tokens, so this sum is the total prompt size (correct for token bookkeeping) but confusing if read as "tokens billed at full price." Not a bug, but the naming is misleading.

## Portability

- **Colon in mined-run identifiers breaks Windows paths.** Run IDs are materialized as directory names under `.hunch/bank/runs/<run_id>/`, and the mined-source runs use a colon-delimited tag form (`:mined:nose_v2_ar`, `:mined:nose_v2_sp`) that becomes a literal directory name with colons. Colon is **illegal in Windows filenames** (reserved as the drive separator) and is remapped by Finder on macOS (POSIX-level it's tolerated, so Linux is unaffected). Confirmed present: `.hunch/bank/runs/:mined:nose_v2_ar` (AR) and `:mined:nose_v2_sp` (SP). All *other* run dirs already use filesystem-safe slugs (`accum-opus46-ar-001`, `ar_v1.1_multi`, `wiki-v1-ar-004`) — only the mined-source naming is affected. Portability is an early priority (second-evaluator recruitment, the on-ramp), so this should be fixed before anyone on Windows/macOS touches a bank. Fix direction: keep the `:mined:...` colon tag as the *logical* ID inside bank records, but sanitize it to a safe slug when used as a path component (and migrate the two existing dirs). Verify nothing reads those dir names by parsing the colons back out.

## Trigger / live-offline consistency

- **Live resume collapses missed turn boundaries into one tick (live ≠ offline).** The v1 trigger (`decide_tick_v1`) is shared across live and offline, but the callers feed it different clocks: offline (`replay/driver.py::_drive_one_event`) feeds `sim_now` from each event's transcript timestamp; live (`run.py` step_once) feeds `time.monotonic()` (wall clock). See the deliberate-but-incomplete split noted in `trigger.py` ~L184–186. During normal live operation wall-clock ≈ timestamp deltas so they agree — but on **resume after downtime**, all buffered events are evaluated at a single `monotonic()` instant, so the 300s debounce treats them as simultaneous and fires ONE catch-up tick over the whole gap, whereas offline fires a tick per turn boundary (spaced by transcript timestamps). Observed on the wiser_persona live run: `[tick t-0267] firing (window 3875..4408)` — ~533 events, one tick, then normal cadence. **Why it matters:** breaks the foundational invariant that offline replay of a transcript reproduces a live run over it (the basis for using offline eval as a proxy for live), and likely *degrades* coverage (one tick over a huge window overruns the accumulator watermark → purging → the critic skims the gap). **Does NOT** corrupt past eval data (all offline) or the replay buffer (raw events stored faithfully; a fresh offline replay recovers correct per-turn ticks). **Fix:** feed the live trigger the event's transcript timestamp (with monotonic clamping) instead of wall-clock — ideally by having `run.py` reuse offline's `_drive_one_event`, so there is one drive path and live = offline fed incrementally. Requires persisting `last_sim_now` in the live checkpoint and a resume-after-gap regression test asserting the same ticks as offline. See `docs/unified_replay_mode.md` §1 (the unification this completes).
