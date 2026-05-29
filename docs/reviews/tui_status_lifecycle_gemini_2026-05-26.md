Keychain initialization encountered an error: Cannot autolaunch D-Bus without X11 $DISPLAY
Using FileKeychain fallback for secure storage.
Loaded cached credentials.
Error executing tool read_file: File not found.
Error executing tool run_shell_command: Tool "run_shell_command" not found. Did you mean one of: "grep_search", "cli_help", "read_file"?
Here is a code review focusing on your mandatory requirements and project policies:

### 1. Silent Failure Modes

**Swallowed transcript errors (`tests/test_hook_stop_delivery.py`, lines 395-408)**
In `_read_session_transcript`, if the computed `transcript_dir` does not exist or contains no `.jsonl` files, the function returns `""`. This violates the "crash loud" policy. If the path encoding logic is wrong or Claude fails to generate a transcript, the test will fail later with a misleading `AssertionError: Injection block not found in session transcript`.
*Fix:* Raise a `FileNotFoundError` explicitly if the directory or `.jsonl` glob returns empty.

**Discarded debugging context on timeout (`tests/test_hook_stop_delivery.py`, lines 539-544)**
In `test_async_rewake_delivery_via_claude_p`, the `except subprocess.TimeoutExpired:` block calls `proc.communicate()` but discards the return values before re-raising. If the test times out, the *remaining* `stdout` and `stderr` from the Claude process (which contains the critical context of what Claude was doing when it stalled) are permanently swallowed.
*Fix:* Capture and attach the output to the raised error:
```python
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(f"Claude process timed out. STDOUT:\n{out}\nSTDERR:\n{err}")
```

### 2. Unnecessary Code Replication

**Transcript verification block (`tests/test_hook_stop_delivery.py`, lines 468-477 and 555-564)**
Both E2E tests perform an identical 4-line assertion block checking for `hid`, the `<hunch-injection>` block, and the smell text in the session transcript.
*Fix:* Extract this block into a shared `_assert_hunch_in_transcript(project_dir, hid, expected_smell)` helper function to prevent drift and reduce boilerplate.

### 3. Correctness & Edge Cases

**Unhandled raw statuses masquerade as "pending" (`hunch/panel.py`, lines 45-56)**
In `display_status`, any unrecognized `record.status` falls through to checking the label, and ultimately returns `"pending"`. According to `docs/framework_v0.md` (lines 215 and 455), `"suppressed"` is a valid explicit status in the replay buffer. If a hunch was suppressed programmatically (lacking a `feedback.jsonl` label), this function will erroneously return `"pending"`. This will force an already-resolved hunch back into the active UI view.
*Fix:* Return `record.status` as the fallback instead of hardcoding `"pending"`, or explicitly map `"suppressed"`.

### 4. Test Coverage Gaps

**Missing assertions for hidden views (`tests/test_panel.py`, lines 142-153)**
The docstring for `test_visible_shows_only_active_by_default` claims it *"hides dismissed/delivered/skipped"*. However, the test setup only emits and labels hunches that end up `pending`, `dismissed`, and `approved`. It completely misses testing that `delivered` and `skipped` hunches are successfully filtered out of the active view.
