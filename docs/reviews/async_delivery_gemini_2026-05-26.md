Keychain initialization encountered an error: Cannot autolaunch D-Bus without X11 $DISPLAY
Using FileKeychain fallback for secure storage.
Loaded cached credentials.
Here is a review of the PR, focusing on correctness, race conditions, silent failures, and code replication. 

There is a major architectural race condition and a silent failure mode that will break the delivery loop in production.

### 1. Correctness & Race Conditions (High Priority)

**The "Thundering Herd" of Watchers (Multiple overlapping processes)**
* **File:** `hunch/hook/stop_delivery.py`, `handle_stop_delivery`
* **Issue:** Because Claude Code fires the `Stop` hook at the end of *every* response, and your watcher lives for `MAX_WAIT_S = 300.0` (5 minutes), an active user will stack up multiple background watchers. If a user completes 10 turns in 5 minutes, there will be 10 background processes executing the 5-second polling loop simultaneously.
* **Impact:** When a hunch is finally approved, *all 10 watchers* will read it, mark it surfaced, write to `stderr`, and exit 2. Claude Code will receive 10 identical `asyncRewake` events, resulting in the same hunch being injected 10 times.
* **Fix:** You must enforce mutual exclusion. The easiest way is to wrap the polling loop in an exclusive file lock (e.g., using `fcntl.flock` on UNIX or a simple lockdir) scoped to the `replay_dir`. If a watcher cannot acquire the lock (because an older watcher is already polling), it should simply exit 0.

**Race Condition: `UserPromptSubmit` vs. `StopDelivery`**
* **File:** `hunch/hook/user_prompt_submit.py` and `hunch/hook/stop_delivery.py`
* **Issue:** If the user sends a prompt at the exact moment the 5-second `stop-delivery` tick wakes up, both hooks read the hunch as `pending`. Both will append `surfaced` to `hunches.jsonl`, and both will deliver the hunch (one via standard `additionalContext`, the other via `asyncRewake` stderr).
* **Fix:** Centralize the read-check-write cycle into a single atomic operation using an exclusive file lock, ensuring only one hook claims the deliverable hunch.

### 2. Silent Failure Modes

**Outer `try/except` kills the watcher on transient errors**
* **File:** `hunch/hook/stop_delivery.py`
* **Issue:** The `try...except Exception:` block completely wraps the `while time.monotonic() < deadline:` loop.
* **Impact:** If `read_current_hunches` experiences a torn read (e.g., a `JSONDecodeError` because another process is actively writing to `hunches.jsonl`), the watcher will catch the exception, print to stderr, return 0, and **permanently die**. The async delivery loop stops entirely until the user triggers another Claude `Stop` event.
* **Fix:** Move the `try/except` block *inside* the `while` loop. Transient read errors should be logged to stderr, but the process should `time.sleep(poll_interval)` and try again on the next tick rather than exiting.

**Unchecked `feedback.jsonl` missing file**
* **File:** `hunch/hook/stop_delivery.py` in `_find_deliverable`
* **Issue:** You check if `hunches_path.exists()`, but unconditionally call `read_labeled_hunch_ids(replay_dir / "feedback.jsonl")`.
* **Impact:** If `read_labeled_hunch_ids` does not swallow `FileNotFoundError` internally, a fresh session (where `feedback.jsonl` hasn't been created yet) will crash the watcher on its very first tick. Because of the outer `try/except` mentioned above, the process exits 0 silently and never polls again.
* **Fix:** Ensure `feedback.jsonl` exists before reading, or confirm `read_labeled_hunch_ids` safely handles missing files.

### 3. Unnecessary Code Replication

**Duplicated `_utc_now_iso()`**
* **File:** `hunch/hook/stop_delivery.py` vs `hunch/hook/user_prompt_submit.py`
* **Issue:** The exact `_dt.datetime.now(_dt.timezone.utc).strftime(...)` logic is copy-pasted. Move this to a shared location (e.g., a shared `hunch.utils` or `hunch.journal.utils` file).

**Duplicated Status Change Loop**
* **File:** `hunch/hook/stop_delivery.py` (`_mark_surfaced`) vs `hunch/hook/user_prompt_submit.py` (`handle_user_prompt_submit`)
* **Issue:** Both files contain an identical 5-line block to instantiate `HunchesWriter` and loop over hunches calling `writer.write_status_change(..., new_status="surfaced")`. This is a copy-paste pattern that will drift.
* **Fix:** Extract a shared `mark_hunches_surfaced(replay_dir: Path, hunches: list[HunchRecord], by: str)` function and use it in both hooks.

### 4. Test Coverage Gaps

* **The Thundering Herd Scenario:** There are no tests in `TestSubprocessIntegration` that spawn multiple concurrent background hooks to verify they don't deliver the same payload multiple times.
* **Transient Read Errors:** There is a test (`test_no_hunches_dir_returns_0_on_error`) that mocks `_find_deliverable` to throw an exception, but it incorrectly validates the *broken* behavior (that the watcher exits 0 immediately). Once you move the `try/except` inside the `while` loop, you should add a test verifying that the loop catches the exception, sleeps, and continues polling.
* **Race Condition (UPS vs. Hook):** No integration test covers the `user_prompt_submit` being called concurrently with the `stop_delivery` hook.

### 5. Behavioral Regressions
* The renaming and exporting of `format_hunch_injection` (formerly `_format_additional_context`) is handled correctly and safely shared between the two delivery vectors. No regressions observed here.
