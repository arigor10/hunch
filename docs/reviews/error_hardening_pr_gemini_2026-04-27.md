Keychain initialization encountered an error: Cannot autolaunch D-Bus without X11 $DISPLAY
Using FileKeychain fallback for secure storage.
Loaded cached credentials.
Excellent work on this PR. The focus on hardening error handling and improving the audit trail is a significant step forward for the project's reliability. The changes are largely well-thought-out and implemented.

I have one critical issue to raise regarding a race condition, along with a few other suggestions for improvement.

### 1. Critical: Race Condition in Monotonicity Guard

The new monotonicity guard in `hunch/journal/hunches.py` is a great idea, but its current implementation is vulnerable to a time-of-check-to-time-of-use (TOCTOU) race condition. This means it does not fully prevent the concurrent-writer scenario it was designed to solve.

**File:** `hunch/journal/hunches.py`
**Methods:** `write_emit`, `write_filtered`

**The Problem:**
The `_check_id_monotonicity` method reads the file to find the max existing ID, and then the `_append` method writes the new record. These two operations are not atomic.

**Attack Scenario:**
1.  **Process A** calls `writer.allocate_id()` and receives `h-0005`.
2.  **Process B** (a concurrent writer) also calls `writer.allocate_id()` and also receives `h-0005`, because Process A has not yet written to disk.
3.  **Process A** begins `write_emit`. It calls `_check_id_monotonicity`, which scans the file and finds the max ID is `h-0004`. The check passes.
4.  The operating system preempts Process A before it can execute `_append`.
5.  **Process B** begins `write_emit`. It also calls `_check_id_monotonicity`, scans the file, and also finds the max ID is `h-0004`. Its check also passes. Process B then successfully appends the record for `h-0005`.
6.  Process A resumes and executes `_append`, writing a *second* record for `h-0005`.

The result is a corrupted `hunches.jsonl` file with duplicate IDs, which is the exact bug the guard was intended to prevent. The existing test, `test_concurrent_writer_detected`, only covers a specific sequential execution and does not reveal this race condition.

**Recommendation:**
To make this guard robust, the check-and-append operation must be atomic. The standard solution is to use a file lock.

```python
# In HunchesWriter, for example:
import fcntl

# Inside write_emit, write_filtered, etc.
with open(self.hunches_path, "a") as f:
    fcntl.flock(f, fcntl.LOCK_EX) # Acquire exclusive lock
    # Your _check_id_monotonicity(hunch_id) logic here.
    # It must read from the file using a different file handle or re-read.
    # A simple way is to perform the scan inside the locked block.
    
    # self._append(record) should now write to this file handle 'f'
    f.write(json.dumps(record) + "\n")
    
    fcntl.flock(f, fcntl.LOCK_UN) # Release lock
```
This is a conceptual sketch. A context manager for the lock would make the implementation cleaner. Without a file lock, this guard provides a false sense of security.

### 2. Test Coverage Gaps

The new tests are excellent, especially the online/offline parity test. However, a few edge cases related to the new guards are untested.

*   **Invalid Hunch ID format:** In `hunch/journal/hunches.py`, `_check_id_monotonicity` silently does nothing if the `hunch_id` doesn't match the `h-NNNN` format. This could mask bugs where malformed IDs are generated. The method should probably raise a `ValueError` if it receives an ID it can't parse.
*   **Malformed `hunches.jsonl`:** The `_scan_max_id()` method's robustness is critical. What happens if `hunches.jsonl` contains a corrupted line (e.g., non-JSON text, or a JSON object without a `hunch_id`)? A test case that attempts to write to a corrupted hunch file would ensure the guard doesn't crash.

### 3. Behavioral Changes (Positive)

The move to propagate exceptions rather than swallow them is a major improvement. I've reviewed the key areas and the changes appear to be handled safely:

*   **`hunch/critic/engine.py`**: The `parse_response` function now raises a `ValueError`, but the calling code in `CriticEngine` correctly catches it and integrates it into the consecutive failure logic. Moving the failure counter reset to *after* a successful parse is a critical and correct fix.
*   **`hunch/filter/core.py`**: Raising exceptions on LLM/CLI failures and unparseable responses is the right move. This makes the pipeline fail loudly instead of producing incomplete or incorrect results. The increased timeout is also a practical improvement.

### 4. Design

The overall design changes are strong.
*   **Filter Backend Configuration:** Decoupling the `HunchFilter` from the LLM call implementation by allowing `dedup_backend` and `novelty_backend` to be injected is a great architectural decision. It improves modularity and testability.
*   **Pre-allocating IDs:** The change in `run.py` and `replay/driver.py` to pre-allocate IDs before filtering is a clever way to ensure filtered hunches have stable IDs and that `duplicate_of` pointers are meaningful.

This is a high-quality PR that significantly improves the robustness of the system. Addressing the race condition in the monotonicity guard will make it truly solid.
