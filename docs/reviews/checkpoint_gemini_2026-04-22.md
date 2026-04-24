Adversarial Review of Hunch Checkpoint/Resume Mechanism
=========================================================

This document provides an adversarial review of the checkpoint and resume mechanism implemented across the Hunch framework. The review focuses on correctness, robustness, consistency, and test coverage.

### Executive Summary

The checkpoint/resume mechanism is a critical feature for both online (`hunch run`) and offline (`hunch replay-offline`) pipelines. The core implementation using a JSON file with an atomic `tmp`+`rename` write is sound. However, significant flaws exist that compromise its reliability.

The most critical issue is a **race condition** in both online and offline modes that can lead to an inconsistent state between the `hunches.jsonl` output and the `checkpoint.json` file. A crash after a hunch is written but before the checkpoint is updated can lead to duplicated work and corrupted results upon resume.

Furthermore, the **online resume path is entirely untested**, and a logic error was found in its state restoration logic. The offline resume tests are good but do not sufficiently verify the correctness of the resumed output.

### Critical Severity Issues

#### 1. Race Condition Leading to Inconsistent State on Crash

In both `hunch/run.py` and `hunch/replay/driver.py`, there is a window of vulnerability between when a hunch is written to disk and when the checkpoint is updated to reflect that action.

*   **Offline (`replay/driver.py`)**: In `run_replay_from_dir`, `_persist_hunches` is called within `_fire_tick`, which writes to `hunches.jsonl`. The `write_checkpoint` call happens later, after `_drive_one_event` returns. If the process terminates between these two calls, `hunches.jsonl` will contain new hunches, but `checkpoint.json` will not have recorded the tick that produced them. On resume, the replay will re-execute the same tick, leading to **duplicate hunches**.
*   **Online (`run.py`)**: A similar issue exists in `Runner.step_once`. `_fire_tick` is called, which writes hunches via the `HunchFilter`. The `_write_checkpoint` call occurs at the very end of `step_once`. A crash between these operations will leave the system in an inconsistent state, where `_tick_counter` and `_hunches_emitted` in the checkpoint are stale, while `hunches.jsonl` has new data.

While the `HunchFilter` may perform deduplication, the core persistence layer should not rely on application-level logic to recover from its own consistency failures. This is a fundamental flaw in the resume mechanism's atomicity.

### High Severity Issues

#### 1. Complete Lack of Test Coverage for Online Resume

The `hunch run` command is stateful and intended for long-running sessions, making its resume capability arguably more critical than the offline version's. However, there are **no tests** in `tests/test_checkpoint.py` or elsewhere that verify the online resume logic in `Runner.__post_init__`.

This is a major gap. Bugs in this path, such as the one identified below, would go completely undetected. The current test suite only provides confidence for the offline replay scenario.

### Medium Severity Issues

#### 1. Incorrect State Restoration in Online `Runner`

In `hunch/run.py`, the `Runner.__post_init__` method contains a logic error when restoring state from a checkpoint.

*   **Line `hunch/run.py:230`**: The code explicitly recalculates the writer's sequence number by reading the entire `conversation.jsonl` file: `self.writer.tick_seq = sum(1 for _ in f)`.
*   **Problem**: The `Checkpoint` object already contains the correct value in `cp.writer_tick_seq`, which is saved by `_write_checkpoint` (line 392).

The current implementation is inefficient (reads the whole file on every startup) and contradicts the purpose of the checkpoint, which is to provide a fast and authoritative source of restored state. The line should be `self.writer.tick_seq = cp.writer_tick_seq`. This bug also highlights the danger of having no test coverage for this critical path.

### Low Severity Issues & Design Feedback

#### 1. Code Replication and Inconsistency

There is noticeable duplication and asymmetry between the online and offline code paths, which can lead to maintenance issues.

*   **Critic/Filter Initialization**: The logic to instantiate and initialize the `Critic` and `HunchFilter` is duplicated between `_cmd_run` and `_cmd_replay_offline` in `hunch/cli.py`. This could be centralized.
*   **Asymmetric Checkpoint Usage**: The `Checkpoint` dataclass has fields used by only one mode. For instance, `last_sim_now` and `bookmark_pre_event` are offline-only, while `parser_line_offset` and `hook_bookmark` are online-only. This suggests the `Checkpoint` object is not a clean abstraction. A better design might involve a base checkpoint with mode-specific nested data structures.

#### 2. Inadequate Assertions in Offline Resume Test

In `tests/test_checkpoint.py`, the test `test_offline_resume_produces_same_result` is a good start, but its assertions are insufficient. It verifies that the *number* of ticks fired in a resumed run matches a full run. It **does not verify that the content** of `hunches.jsonl` is identical. A correct resume should produce a byte-for-byte identical result. The test should be strengthened to compare the final output files.

#### 3. Unused `events_consumed` Field in Checkpoint

In `hunch/checkpoint.py`, the `checkpoint_from_trigger_state` function is used by the online `Runner` to create checkpoints. It accepts an `events_consumed` parameter but it is always called with the default value of 0. This field is subsequently written to the checkpoint but is never used by the online resume logic. This constitutes dead code in the online path and adds to the confusion around the `Checkpoint` dataclass's responsibilities.

### Recommendations for Missing Tests

1.  **Online Resume Test**: Create a test that:
    a. Instantiates a `Runner` and runs `step_once()` multiple times to generate events, hunches, and a checkpoint.
    b. Simulates a restart by creating a new `Runner` instance for the same `replay_dir`.
    c. Asserts that all relevant state fields (`parser_state.line_offset`, `writer.tick_seq`, `_tick_counter`, `_hunches_emitted`, `trigger_state`, `_hook_bookmark`) are correctly restored from the checkpoint.

2.  **Crash Consistency Test**: A test should be designed to simulate a crash after `_fire_tick` but before `_write_checkpoint`. On resume, it should verify that either:
    a. No duplicate hunches are generated (ideal).
    b. The system explicitly documents that it relies on the `HunchFilter` for idempotency, and the test verifies the filter successfully prevents duplicates.

3.  **Output Identity Test**: The existing `test_offline_resume_produces_same_result` should be augmented to perform a file content comparison on `hunches.jsonl` between the fully-run version and the partially-resumed version.
