# Filter v1 — Gemini 2.5 Pro Adversarial Review (2026-04-22)

Model: gemini-2.5-pro

## Findings

1.  **CRITICAL: Offline replay pipeline (`replay-offline`) does not use the filter.**
    The `hunch replay-offline` command, which is the entry point for the offline evaluation pipeline, does not instantiate or enable the `HunchFilter`. This directly contradicts the design goal that both online and offline pipelines should have identical filter flows. The live `hunch run` pipeline correctly enables the filter by default. This inconsistency undermines the fidelity of offline evaluations, as they will report hunches that the live system would have filtered.

    *   `hunch/cli.py`: In the `_cmd_replay_offline` function, the calls to `run_replay_from_claude_log` and `run_replay_from_dir` do not pass a `hunch_filter` argument.
    *   `hunch/replay/driver.py`: The `run_replay...` functions accept an optional `hunch_filter: HunchFilter | None = None`. If it's `None`, filtering is skipped inside `_persist_hunches`.
    *   `docs/framework_v0.md` & `docs/eval_infrastructure.md`: Both documents explicitly state that the offline/eval pipeline uses the same filter as the live one. The implementation does not match the documentation.

2.  **MAJOR: Inconsistent LLM call mechanism between pipelines.**
    While the filter is unused in the offline pipeline, a second major inconsistency exists. The online pipeline (`hunch run`) appears to exclusively use the `claude` CLI for filter model calls, as no Anthropic SDK client is instantiated for it in `hunch/cli.py`. The filter code (`hunch/filter/core.py`) contains logic for both SDK and CLI calls, but the primary online path only exercises the CLI fallback. If the offline pipeline were fixed to use the filter, it's also unclear how it would be configured to use the SDK. This reliance on subprocess-based CLI calls for the live filter introduces performance overhead and creates a potential behavior discrepancy if the SDK and CLI were to diverge.

    *   `hunch/cli.py`: The `_cmd_run` function instantiates a `Runner`, which in turn creates a `HunchFilter` without an SDK `client`.
    *   `hunch/filter/core.py`: The `_call_llm` function defaults to `_call_via_cli` when `client` is `None`.

3.  **MINOR: Sequential execution of dedup checks introduces latency.**
    The deduplication check in `_check_dedup` iterates through the window of prior hunches and performs a blocking, synchronous LLM call for each one. These comparisons are independent and could be executed in parallel. For a hunch that is truly novel, this results in up to `dedup_window` (default 10) sequential network requests, adding significant latency to the live monitoring loop for no benefit.

    *   `hunch/filter/core.py`: The `for prior in reversed(window):` loop inside `_check_dedup` makes a serial `_call_llm` on each iteration.

4.  **MINOR: Test suite is missing integration tests for CLI commands.**
    The tests in `tests/test_filter.py` effectively unit-test the `HunchFilter`'s logic in isolation. However, there are no integration tests that invoke the high-level CLI commands like `hunch run` and `hunch replay-offline` to verify their complete end-to-end behavior. The lack of such tests allowed the critical bug—where the filter is disabled in the offline pipeline—to go undetected.

    *   `tests/test_filter.py`: Contains only unit tests for filter logic.
    *   `hunch/cli.py`: The commands defined here are not covered by end-to-end tests that would verify filtering behavior.

5.  **NIT: Hardcoded character limit in dialogue rendering for novelty check.**
    The `_render_dialogue` function in `hunch/filter/core.py` uses a hardcoded `_MAX_CONTEXT_CHARS` constant (80,000) and truncates the beginning of the dialogue if it exceeds this length. While a necessary safeguard, this value is not configurable. In extremely long-running sessions, this could cause the novelty check to miss a concern that was raised very early in the conversation, leading it to incorrectly pass a redundant hunch.

    *   `hunch/filter/core.py`: The `_MAX_CONTEXT_CHARS` constant and its use in `_render_dialogue`.

6.  **NIT: Filter model names use future dates.**
    The model names specified in `hunch/filter/core.py`, `claude-haiku-4-5-20251001` and `claude-sonnet-4-5-20250929`, contain dates in 2025. This appears to be a project-specific naming convention for placeholder or unreleased models, but it could cause confusion or runtime errors if these are not valid model identifiers in the target environment.

    *   `hunch/filter/core.py`: The `DEDUP_MODEL` and `NOVELTY_MODEL` constants.
