Keychain initialization encountered an error: Cannot autolaunch D-Bus without X11 $DISPLAY
Using FileKeychain fallback for secure storage.
Loaded cached credentials.
Here is a review of the diff, focusing on correctness, edge cases, and the project's specific design policies.

### 1. Silent Failure Modes (Policy Violation)
The PR explicitly violates the `"Crash loud, never swallow silently"` policy in three different places by catching and ignoring malformed JSON.
* **`hunch/hook/user_prompt_submit.py` (lines 246-249)** in `_read_max_tick_seq`. 
* **`hunch/journal/feedback.py` (lines 209-212)** in `read_hunch_responses`.
* **`hunch/journal/feedback.py` (lines 235-238)** in `read_hunch_reminders`.

```python
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # <--- SILENT SWALLOW
```
If a line is corrupted, continuing silently masks data loss. Remove the `try...except` block so the JSON parser crashes loudly on malformed files.

### 2. Logic Errors & Edge Cases
* **Regex leak grabs IDs from reminders (Race/Edge Condition):** 
  In `hunch/parse/transcript.py` (lines 314-316), `_extract_hunch_ids` applies a global regex `r"\[h-\d{4}\]"` to the entire payload. As proven by `test_injection_and_reminder_coexist`, the `UserPromptSubmit` hook can bundle both a `<hunch-injection>` and a `<hunch-reminder>` block into a single turn context. When this payload is parsed, the global regex will incorrectly extract the IDs of *reminded* hunches and emit them as newly injected in the `hunch_injection` event. You must restrict the extraction to text exclusively bound between the `<hunch-injection>...</hunch-injection>` tags.

* **Brittle delivery hook detection:** 
  In `hunch/parse/transcript.py` (line 239), distinguishing the delivery hook relies on:
  `hook = "stop_delivery" if "Stop" in rec["text"] else "user_prompt_submit"`
  This is extremely brittle. If a hunch's smell or description happens to contain the word "Stop" (e.g. `[h-0004] Stop using stale calibration data`), it will falsely flag the delivery as `stop_delivery`. Change this to check for the structural signature instead (e.g. `"Stop hook" in rec["text"]` or `"<summary>Stop hook</summary>"`).

### 3. Unnecessary Code Replication
* **Duplicating `read_max_tick_seq`:**
  In `hunch/hook/user_prompt_submit.py` (lines 236-253), you wrote `_read_max_tick_seq` from scratch. This exact function already exists in the codebase! As seen in `hunch/panel.py` (line 135), the original `read_max_tick_seq` is already available to be imported and used. Drop the local implementation and import the existing one.

* **Duplicating the JSONL iteration loop:**
  The `with open(path) as f: for line in f:` parsing boilerplate is copy-pasted into `read_hunch_responses` and `read_hunch_reminders` (and likely mirrors `read_labeled_hunch_ids` and `read_hunch_edits`). This is why the `JSONDecodeError` bug was replicated three times. Abstract this into a common `iter_jsonl(path)` helper to yield parsed dictionaries, drying up the file I/O and enforcing a single failure policy.
