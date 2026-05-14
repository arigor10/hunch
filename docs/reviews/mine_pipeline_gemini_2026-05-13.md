Keychain initialization encountered an error: Cannot autolaunch D-Bus without X11 $DISPLAY
Using FileKeychain fallback for secure storage.
Loaded cached credentials.
Here is my review of the `hunch mine` pipeline implementation. I have focused on correctness, code reuse, and readiness for external use, as requested.

### Executive Summary

The implementation is a solid first draft that largely follows the design document. However, it has several critical issues that must be addressed before it can be considered robust or ready for use by external developers. The most significant problems are:

1.  **A complete lack of tests** for the core logic in `nose.py` and `evidence.py`.
2.  A **significant security risk** in `evidence.py` due to the default use of a dangerous permission-skipping flag.
3.  **Flawed checkpoint/resume logic** in `nose.py` that fails to cache results across successful runs, defeating its purpose for iterative development.
4.  **Extensive code duplication**, which increases the maintenance burden.
5.  **Silent error handling** that violates the project's "crash loud" policy and can lead to incomplete results being reported as a success.

Below is a detailed breakdown of the issues.

---

### 1. Correctness & Test Coverage

The most critical issue is the lack of testing for the primary application logic.

*   **Missing Tests for Core Logic:**
    *   **Files:** `hunch/mine/nose.py`, `hunch/mine/evidence.py`
    *   **Issue:** There are no tests for the main `run_nose_mining` and `run_evidence_mining` functions. This means the complex and stateful logic for chunking, making LLM calls, checkpointing, deduplication, and workspace management is completely unverified.
    *   **Recommendation:** Add comprehensive tests for both modules. The LLM/agent calls should be mocked to allow for fast, deterministic testing of the surrounding logic, including error handling and checkpointing behavior.

*   **Flawed Checkpoint/Resume Logic in `nose.py`:**
    *   **File:** `hunch/mine/nose.py`, functions `_load_checkpoint` and `_write_findings`.
    *   **Issue:** The checkpoint mechanism relies on a temporary `_chunk_key` field. The `_write_findings` function strips any key starting with `_` from the final `findings.jsonl`. Consequently, if a run completes successfully and the user launches a second run, `_load_checkpoint` will find no `_chunk_key`s and will reprocess all chunks from scratch. The caching only works for resuming a *failed* run, not for iterating quickly with different parameters.
    *   **Recommendation:** Modify `_write_findings` to persist the `_chunk_key`. Alternatively, if the goal is only to resume failed runs, this behavior should be explicitly and clearly documented, as it is highly counter-intuitive.

*   **Weak Tests for `chunker.py`:**
    *   **File:** `tests/test_mine_chunker.py`
    *   **Issue:** The existing tests are too high-level. For example, `test_user_turn_boundary_snapping` only confirms that chunks are produced, not that they are correctly aligned to user turn boundaries.
    *   **Recommendation:** Add specific test cases with known event sequences and assert the exact `start_seq` and `end_seq` of the resulting chunks to validate the boundary-snapping and overlap logic.

### 2. Security

*   **Dangerous Default Permissions in `evidence.py`:**
    *   **File:** `hunch/mine/evidence.py`, function `_run_agent`.
    *   **Issue:** The `claude` agent is invoked with the `--dangerously-skip-permissions` flag by default. This grants the LLM agent broad, unsupervised file system access within the workspace, which is a major security concern for any user of this tool. The design doc makes no mention of this.
    *   **Recommendation:** This flag must not be the default. The program should require the user to explicitly opt-in via a CLI flag (e.g., `--allow-dangerous-agent-access`). The help text for this flag must clearly explain the security implications.

### 3. Code Reuse & Maintenance

There is significant and unnecessary code duplication that will make future maintenance difficult.

*   **Redundant Rendering Logic:**
    *   **Files:** `hunch/mine/renderer.py` and `hunch/critic/wiki_renderer.py`.
    *   **Issue:** These two files are nearly identical. Functions like `_render_event`, `_read_snapshot`, and `_truncate` are duplicated. The minor differences in output formatting (`[Scientist] (seq NN):` vs. `**USER:**`) do not justify maintaining separate files.
    *   **Recommendation:** Consolidate the logic into a single, shared rendering module in a neutral location. The renderer function can accept a parameter to control the output style (e.g., `style='mining'` vs. `style='critic'`).

*   **Redundant Claude CLI Invocation:**
    *   **Files:** `hunch/mine/nose.py` and `hunch/backend/claude_cli.py`.
    *   **Issue:** The `_call_mining_llm` function in `nose.py` reimplements the logic for calling the `claude` CLI, which is already encapsulated in `hunch.backend.claude_cli.ClaudeCliBackend`.
    *   **Recommendation:** Refactor `nose.py` to use `ClaudeCliBackend`. This will centralize the CLI interaction, reduce code, and simplify testing by allowing the backend to be mocked.

### 4. Silent Failure Modes & Error Handling

The implementation violates the project's "crash loud" policy by swallowing errors.

*   **Swallowed Exceptions in Core Loops:**
    *   **Files:** `hunch/mine/nose.py` (`run_nose_mining`), `hunch/mine/evidence.py` (`run_evidence_mining`).
    *   **Issue:** Both modules wrap their core processing loops (for chunks and findings, respectively) in a broad `try...except Exception`. On error, they log a message and continue. If a persistent error occurs (e.g., authentication failure), every unit of work will fail, but the script will run to completion and exit successfully.
    *   **Recommendation:** Introduce a failure threshold. For instance, if more than N consecutive chunks/findings fail, abort the entire run and exit with a non-zero status code. This provides a stronger failure signal while still tolerating transient issues.

*   **Silently Dropped Data in `nose.py`:**
    *   **File:** `hunch/mine/nose.py`, function `_parse_findings`.
    *   **Issue:** The function silently ignores lines that fail to parse as JSON (`except json.JSONDecodeError: continue`). If the LLM produces a mix of valid and malformed JSON, the malformed findings are simply discarded without any notification to the user.
    *   **Recommendation:** Log a warning to `stderr` when a JSON parsing error occurs, including the problematic line.

### 5. Design Doc Compliance

*   **Discrepancy in Write Strategy:**
    *   **Files:** `hunch/mine/evidence.py`, design doc.
    *   **Issue:** The design doc states, "The output file is written via tmp+rename, with a `.bak` copy." `evidence.py` instead appends to `hunches.jsonl` directly (`_append_hunch`).
    *   **Recommendation:** The append strategy is superior for the incremental nature of evidence mining. The design document should be updated to reflect the implemented behavior.

### 6. External User Readiness

*   **Implicit Dependencies:** The reliance on the `claude` CLI tool being installed and in the `PATH` should be explicitly stated in the project's main `README.md` or usage documentation.
*   **Unusual `cwd`:** The use of `cwd="/tmp"` in `nose.py` is undocumented and potentially confusing. The rationale should be explained in a code comment, and it should ideally use a project-specific temporary directory rather than the system-wide `/tmp`.
