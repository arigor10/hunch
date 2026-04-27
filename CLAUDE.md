# Hunch

Real-time research critic that watches Claude Code sessions and flags anomalies.

## Error handling policy

**Crash loud, never swallow silently.** A run that completes "successfully" but produces garbage is worse than a run that crashes with a clear error. We learned this the hard way: silent exception swallowing let the filter run for 130+ ticks doing nothing, burning inference budget on unfiltered output.

Rules:

1. **Never `except Exception: return None/""/{}/[]`** without logging. If you catch a broad exception, log it. If the failure means the output is degraded (e.g., a filter check wasn't performed), the log line must make that obvious.

2. **Prefer raising over returning sentinels.** A function that returns `""` on error is indistinguishable from a function that legitimately found nothing. Raise an exception and let the caller decide whether to retry, count failures, or abort.

3. **Count consecutive failures and abort.** The engine's `_consecutive_failures` counter is the pattern: transient errors get retried, but persistent errors (broken API key, wrong model, unparseable output) escalate to RuntimeError after N attempts. Parse failures feed into this counter too.

4. **Validate eagerly, not lazily.** Check API keys, config values, and prompt templates at startup, not on first use. `anthropic.Anthropic()` succeeds without a key — the call fails later. Check `ANTHROPIC_API_KEY` before creating the client. Validate model names are non-empty. Validate prompt templates have the expected marker.

5. **Hooks are the one exception.** Claude Code hooks (`stop.py`, `user_prompt_submit.py`) must never crash — a hook failure kills the user's prompt. Log to stderr and return gracefully. But still log.

6. **Distinguish "nothing found" from "check failed".** A filter returning "not a duplicate" must mean the check ran and found no match, not that the LLM call timed out. Use exceptions to signal "check didn't run."

## Running tests

```
cd /home/arigor/YoC/hunch && python3 -m pytest tests/ -x -q
```

## Key architecture

- `hunch/critic/engine.py` — model-agnostic critic engine, owns the tick loop
- `hunch/critic/accumulator.py` — sliding-window prompt stream with purge compaction
- `hunch/backend/` — pluggable backends (claude_cli, anthropic_sdk, openrouter)
- `hunch/filter/core.py` — post-critic dedup + novelty filter
- `hunch/replay/driver.py` — offline replay pipeline
- `hunch/run.py` — online (live session) pipeline
- `hunch/hook/` — Claude Code hooks (stop, user_prompt_submit)
