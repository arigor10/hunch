# Hunch — Framework v0 Design

*Companion to VISION.md. Describes the v0 framework skeleton: the integration spine that the Critic rides on. The Critic's own internals are out of scope here.*

---

## Purpose & scope

This doc specifies the **Framework v0**: the minimum plumbing required for a single end-to-end loop where a Scientist works with a Researcher (Claude Code), a Critic observes in parallel, and hunches surface to the Scientist via a side panel and can be passed to the Researcher.

**In scope:** Capture, trigger, Surface (side panel + hook), Feedback, Config, and the *interface* the Critic speaks to the framework.

**Out of scope (intentionally deferred):** What the Critic actually does with its input (prompts, cadence tuning, window size, confidence bar). The Critic is a black box behind its interface contract.

**Anchor:** v0 skeleton is due **2026-04-24** for first users to try. Quality is not the bar; end-to-end runnable is.

## Guiding principle

Build the skeleton so that every component we ship is **additive toward** the long-term vision from VISION.md — especially mid-turn interjection, agentic Critic, and learning by mentorship — and **never requires a rewrite** to get there. This doc flags, for each component, the future capabilities it must not preclude.

---

## Architecture at a glance

```
┌──────────────────────────────────────────────────────────────────┐
│  tmux session  (the "Surface")                                   │
│  ┌─────────────────────────────┐   ┌──────────────────────────┐  │
│  │  Main pane: Researcher      │   │  Side pane: Hunch UI     │  │
│  │                             │   │                          │  │
│  │  $ claude                   │   │  hunches stream:         │  │
│  │  [Scientist ↔ Researcher]   │   │  ─────────────────       │  │
│  │  ↑ UserPromptSubmit hook    │   │  [H-0042] ...            │  │
│  │    prepends unread hunches  │   │  [g]ood [b]ad [s]kip     │  │
│  │                             │   │  [i]nject now            │  │
│  └─────────────────────────────┘   └──────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
        │  CC transcript                           ▲       │
        ▼                                          │reads  │writes
  ┌────────────┐       writes         ┌───────────────────────────┐
  │  Capture   │ ───────────────────▶ │     .hunch/replay/        │
  └────────────┘                      │ ├─ conversation.jsonl     │
                                      │ ├─ artifacts.jsonl        │
  ┌────────────┐       tick           │ ├─ artifacts/             │
  │  Trigger   │ ──────────┐          │ ├─ hunches.jsonl          │
  └────────────┘           ▼          │ └─ feedback.jsonl         │
                    ┌────────────┐    └───────────────────────────┘
                    │  Critic    │        ▲           ▲
                    │  process   │ ───────┘ writes    │ reads/writes
                    └────────────┘                    │
                         ▲ reads                      │
                         └──────────────────┬─────────┘
                                     (same files, one source of truth)
```

**Five load-bearing components:**

1. **Capture** — writes the replay buffer
2. **Trigger** — decides when the Critic evaluates
3. **Critic** (interface only, implementation deferred) — reads replay buffer, writes hunches
4. **Surface** — shows hunches to the Scientist; prepends approved hunches to Researcher's next prompt
5. **Feedback** — records Scientist reactions (explicit keys + implicit reply)

Plus cross-cutting: **Config** (paths, scaffolding) and the **Replay buffer** (the central data artifact).

---

## Design invariants (doors we refuse to close)

These are the interface-level commitments we take to the bank. Everything in v0 is implementable without violating them, and every future extension we've sketched fits behind them.

1. **The replay buffer is the single source of truth for Critic input.** Anything the Critic reads must be in, or referenced from, `.hunch/replay/`. The Critic must never need to reach outside this directory for primary data.
2. **The Critic is a process, not a function.** Launched by the framework via a configured command; communicates over stdio JSON (or a Unix socket). Stateful and stateless implementations both fit.
3. **Each Critic tick carries `(full_snapshot_bookmark, delta_bookmark)`.** The Critic either re-reads full or reads only since-bookmark. The framework doesn't care which.
4. **All replay-buffer JSONL files are strictly append-only.** This includes `hunches.jsonl`: a hunch's lifecycle (pending → shown_to_researcher → suppressed, etc.) is represented as a sequence of *status-change event* entries appended by whoever changes status. Current state is computed by folding events. No in-place mutation anywhere; every consumer (side panel, hook, future agentic Critic, future analytics) reads the same files and gets a full audit trail for free.
5. **Surface is file-triggered, not call-triggered.** Anything that wants to display, inject, or react to a hunch reads `hunches.jsonl` and `feedback.jsonl`. This lets v0's side panel, v0.5's PreToolUse hook, and v1's Stop hook all coexist and all consume the same data.
6. **Hunch → Researcher injection happens via `UserPromptSubmit` hook in v0**, but the hook is a thin reader over `hunches.jsonl`. Swapping to `PreToolUse`/`Stop` later changes *when* prepending fires, not *what* is prepended.
7. **Config is layered: required-interface < auto-discovery < `hunch init` scaffolding.** Zero-config usage must be possible for common layouts; opinionated scaffolding is optional.

If we're ever tempted to violate one of these to save time in v0, stop and rethink — we're about to close a door.

---

## Components

### 1. Capture

**v0:** A background process that polls the Claude Code transcript (`~/.claude/projects/<encoded>/<session>.jsonl`) and the configured artifact directories every N seconds (default 3s). New transcript entries are parsed into chunks (reusing the existing parser in the sibling project) and appended to `.hunch/replay/conversation.jsonl`. Artifact writes/edits are snapshotted into `.hunch/replay/artifacts/` and logged in `.hunch/replay/artifacts.jsonl`. Figures (`.png`, `.jpg`, `.svg`) are copied as binary snapshots into the same artifacts dir.

**Contract:**
- Writes only to `.hunch/replay/`, never reads from it.
- Append-only semantics — an entry, once written, is immutable.
- Each entry carries a monotonic `tick_seq` so consumers can use bookmarks.
- Artifacts referenced by relative path from the artifacts/ dir, never absolute.

**Future extensions (doors left open):**
- Swap polling for `watchdog` (fsevents on macOS, inotify on Linux) — identical file outputs.
- Capture multiple Researcher sessions to the same replay buffer (for parallel work).
- Richer artifact types (notebooks, CSVs with change summaries).
- Capture from non-Claude-Code Researchers (any tool that writes to a known transcript format) — Capture becomes pluggable.

### 2. Trigger

**v0:** Time-based. Every N seconds (default 10s), the framework sends a tick to the Critic process if there are new replay entries since the last tick. Otherwise skip. If the Critic is still mid-tick when the next fire time arrives, skip (log a `skipped_busy` event).

**Contract:**
- Tick is a JSON message on the Critic's stdin: `{"tick_id", "bookmark_prev", "bookmark_now"}`.
- The framework guarantees at most one in-flight tick per Critic process.
- Skip policy (`skip` vs `queue` vs `kill_and_restart`) is config.

**Future extensions:**
- Event-based triggering: fire on artifact writes, figure updates, long Researcher monologues, specific conversation patterns.
- Critic self-triggering: agentic Critic runs continuously, emits hunches when *it* decides. Framework downgrades to notification-only.
- Cadence-learning: trigger policy tunes itself based on hunch acceptance rates in different conversation phases.

### 3. Critic (interface only)

The Critic is a black box behind its protocol. v0 will ship a stub implementation; its internals are specified separately (see `critic_v0.md` — to be written).

**Contract (the Critic protocol):**

Launched by the framework via configured command (see Config section). Communicates over stdio JSON.

```
startup:
  framework → critic: {"type": "init", "config": {...}}
  critic → framework: {"type": "ready"}

per tick:
  framework → critic: {"type": "tick", "tick_id": "...",
                       "bookmark_prev": int, "bookmark_now": int}
  critic → framework: {"type": "tick_result", "tick_id": "...",
                       "hunches": [Hunch, ...],
                       "debug": {...} (optional)}

per mentorship tick (v0.5+, contract reserved now):
  framework → critic: {"type": "mentorship_tick",
                       "hunch_id": "...",
                       "scientist_message": "..."}
  critic → framework: {"type": "mentorship_reply",
                       "hunch_id": "...",
                       "reply": "..."}

shutdown:
  framework → critic: {"type": "shutdown"}
  critic → framework: {"type": "bye"}
```

The Critic has read access to the entire `.hunch/replay/` directory. It pulls whatever subset of the replay buffer it wants per tick. Hunches are returned in the tick result **and** may be appended directly to `hunches.jsonl` by the Critic (both paths must be supported — the framework dedups by `hunch_id`).

**v0 implementation (sketched, detailed in `critic_v0.md`):**
- In-process Python wrapper around a Sonnet API call.
- Reads the last N chunks + current artifact snapshots from replay buffer.
- Uses a port of the offline v2 mining prompt.
- Stateless; relies on `prior_hunches` in its own input to avoid repeats.
- Anthropic prompt caching on the stable prefix.

**Future extensions (doors left open):**
- Long-running agentic Critic: `claude -r <session-id> -p "<tick message>"` per tick, or SDK-hosted agent. Maintains its own scratchpad tree of principles, updated as it goes. Identical protocol on the wire.
- Fine-tuned model backend.
- Ensemble of Critics with principle-merging across them (see VISION § Mergeability).
- Mentorship dialogue driven by the second tick type already reserved above.

### 4. Surface

**v0:** A tmux layout with two panes:
- **Main pane** runs `claude` (the Scientist's Researcher session).
- **Side pane** runs `hunch watch`, a TUI that tails `hunches.jsonl`, renders each hunch with its id and smell description, and captures keyboard shortcuts.

Keyboard shortcuts (tmux no-prefix bindings, delivered via `bind-key -n`):
- `Alt-g` — mark latest unresolved hunch as *good*, will be prepended to Researcher on next natural turn.
- `Alt-b` — mark as *bad*, suppress prepend.
- `Alt-s` — mark as *skip*, suppress prepend.
- `Alt-i` — mark as *good and inject now* — writes label, then `tmux send-keys -t <researcher_pane> Enter` to trigger a turn with no extra Scientist input, causing the UserPromptSubmit hook to fire and prepend the hunch.

All four keybindings can fire regardless of which pane has focus — the Scientist's cursor never leaves the main terminal.

**Injection mechanism (v0):** A `UserPromptSubmit` hook configured in `~/.claude/settings.json` (or project-local settings) reads `hunches.jsonl`, finds entries with status `good-pending-inject` or (by default) `pending-shown`, formats them as a prefix block like:

```
[Critic hunch, 2 min ago]
<hunch text>
[/Critic hunch]
```

...and injects them as `additionalContext` ahead of the Scientist's message. The hook then marks those hunches as `shown_to_researcher` in `hunches.jsonl`.

**Contract:**
- Side panel reads `hunches.jsonl` (folding events to derive current status); writes to `feedback.jsonl` and appends status-change events to `hunches.jsonl` (e.g. `{type: "status_change", hunch_id, new_status: "suppressed", by: "scientist_key"}`).
- UserPromptSubmit hook reads `hunches.jsonl`, prepends hunches whose folded status is pending-and-not-suppressed, and appends a status-change event (`new_status: "shown_to_researcher"`).
- Neither the side panel nor the hook ever talks directly to the Critic — they only read/write files.

**Future extensions (doors left open):**
- **Mid-turn injection** via a `PreToolUse` or `PostToolUse` hook added alongside UserPromptSubmit. Same file, different trigger point.
- **Guaranteed delivery** via `Stop` hook with `decision: block` — prevents the Researcher from ending its turn while there are unread hunches.
- **SDK-wrapped session** for true mid-tool injection. The Surface abstraction (file-driven) means only the injection layer changes.
- **Side-panel dialogue mode** (mentorship): `Alt-?` opens a text prompt, sends a `mentorship_tick` to the Critic, renders reply inline. Dialogue log goes to `.hunch/mentorship/<hunch_id>.jsonl`.
- Mouse / click support in the side panel TUI.
- Slack / web UI surfaces that consume the same files.

### 5. Feedback

**v0:** Dual-channel:

- **Explicit** — side-panel keys write to `feedback.jsonl`:
  `{"hunch_id", "label": "good"|"bad"|"skip", "ts"}`.
- **Implicit** — when the UserPromptSubmit hook prepends a hunch, it records in `feedback.jsonl`:
  `{"hunch_id", "label": "implicit", "scientist_reply": <the prompt text>, "ts"}`.

Both channels append. No deletion.

**Contract:**
- `feedback.jsonl` is append-only. A hunch may accumulate multiple feedback entries (explicit + implicit, or multiple implicit if it comes up again).
- The Critic reads `feedback.jsonl` on each tick via `prior_hunches` context to avoid repeating suppressed hunches.

**Future extensions:**
- **Mentorship dialogue** — full back-and-forth log per hunch, stored separately (`.hunch/mentorship/<hunch_id>.jsonl`) but conceptually a third feedback channel.
- **Principle extraction** — dialogues produce principles the Critic writes to its own scratchpad; those principles are mergeable/transferable (see VISION § Mergeability).
- **Retroactive feedback** — Scientist can, hours or days later, retroactively label a hunch that was silently ignored.

### 6. Config & scaffolding

**Three layers (per invariant #7):**

- **Required interface (Layer 1):** Hunch needs to know two paths — the Claude Code transcript location (auto-detected from CC conventions) and the artifact directories to watch. That's the minimum.
- **Auto-discovery (Layer 3):** If no `hunch.config.toml` is present, Hunch scans the working directory for `.md` files (respecting `.gitignore`) and common figure dirs (`figures/`, `figs/`, `images/`, `plots/`). Shows discovered paths to user on first run with a confirm-or-edit prompt.
- **`hunch init` scaffolding (Layer 2):** Drops a starter `CLAUDE.md` encoding the recommended research-cycle habits, a default directory layout, a tmux config fragment at `~/.hunch/tmux.conf` (with pane layout, keybindings, and the side-panel launcher), and a populated `hunch.config.toml`. The user adds a single `source-file ~/.hunch/tmux.conf` line to their own `~/.tmux.conf` — we **source**, we do not **merge** (i.e., we never rewrite or edit the user's tmux config in place). Uninstalling Hunch is a one-line comment; our config is independently updatable without touching the user's settings.

**Config file (`hunch.config.toml`) v0 schema:**

```toml
[paths]
transcript = "~/.claude/projects/<auto-detected>"
artifacts  = ["writeups/", "figures/"]
replay     = ".hunch/replay/"

[critic]
command       = "python -m hunch.critic.stateless_sonnet"
tick_interval = 10   # seconds
tick_timeout  = 30   # seconds
on_busy       = "skip"

[surface]
tmux_pane_main   = "researcher"
tmux_pane_side   = "hunch"
hook_script      = "~/.claude/hooks/hunch_inject.sh"
```

**Tmux setup is independent of the research loop.** The pane layout, keybindings, and side-panel launcher are pure UI plumbing. They observe nothing from the Researcher's conventions and require no cooperation from the Scientist's CLAUDE.md or directory structure. The research loop is unaware of Hunch; Hunch observes it. (The one place framework wiring does touch the Researcher side is the `UserPromptSubmit` hook — configured in Claude Code's own settings, not in anything the research loop provides.)

**Portability note (macOS + Linux):**
- tmux, Python, Claude Code, Anthropic API all work cleanly on both.
- All scripts in Python (no GNU-specific shell flags).
- File-watching via polling in v0 avoids inotify vs. fsevents split; `watchdog` library covers both when we upgrade.

**Future extensions:**
- `hunch init` variants per research domain (ML, comp-bio, etc.) with different CLAUDE.md starter templates.
- Support for non-Claude-Code Researchers via a transcript adapter interface.
- Remote deployment (Scientist on laptop, Researcher on GPU box).

---

## Key design decisions (and why)

Preserving rationale here so we don't re-litigate later.

**D1: Two terminals + UserPromptSubmit hook, not inline interjection.**
Real inline interjection (third voice in the transcript) is not a primitive Claude Code exposes today. The closest mechanism is `UserPromptSubmit` `additionalContext`, which makes the hunch appear to the Researcher on the Scientist's next turn. This is *between-turn* interjection — matches the meeting-room analogy ("colleague raises hand at a natural pause") and avoids heavy SDK wrapping. Mid-turn injection is an additive future step via `PreToolUse`/`PostToolUse`.

**D2: Replay buffer as the architectural spine.**
We need the replay buffer anyway (for offline analysis, for the Critic infra, for figure preservation). Making it the framework's central data artifact unifies Capture, Critic input, and everything downstream. It also means the Critic can be stateless, stateful, or another agent without changing what "Critic input" means — it's always a read of the replay directory.

**D3: Critic as a process speaking a protocol over stdio.**
Supports every Critic implementation we can foresee: stateless LLM call, stateful long-running agent, binary, fine-tuned model. For v0 the "process" is an in-process Python call (same signature), but the abstraction is in place.

**D4: Tick carries both full bookmark and delta bookmark.**
Stateless implementations re-read the full window; stateful implementations read only the delta. One contract, both patterns supported.

**D5: File-based Surface, not call-based.**
Keeps every future injection mechanism (side panel, UserPromptSubmit, PreToolUse, Stop hook, SDK) consuming the same `hunches.jsonl`. Adding a new mechanism is always additive.

**D6: Dual-channel feedback (explicit + implicit).**
Explicit gives clean labels for the learning loop; implicit captures *how* the Scientist acted on the hunch (often richer). Both feed the mentorship loop later.

**D7: Explicit-bad/skip suppresses prepend.**
UX correctness: if a hunch is bad, it shouldn't pollute the Researcher's context. The suppression gate lives in the UserPromptSubmit hook, which checks hunch status before prepending.

**D8: Tmux cross-pane keybindings with `send-keys`.**
Lets the Scientist control the side panel (good/bad/skip/inject) without leaving the main terminal. `send-keys` is tmux-native and cross-OS. The `Alt-i` (inject-now) key uses `send-keys Enter` to trigger a turn on the Researcher with no additional input, leveraging the UserPromptSubmit hook to prepend the hunch — elegant reuse of the injection mechanism.

**D9: Scaffolding as carrot, not stick.**
Hunch is positioned in VISION as a listener that "slots in alongside" whatever Researcher the Scientist uses. Prescribing methodology contradicts that. Layered config (auto-discovery → small config → opinionated init) keeps zero-friction for users adopting the defaults while leaving the door open for Scientists with idiosyncratic workflows.

**D10: `prior_hunches` in every Critic tick input.**
Cheap, underrated. Lets even stateless Critics avoid repeating themselves and learn (weakly) from past feedback without session state.

---

## Deferred decisions (with revisit triggers)

Things we chose not to decide now, and what would make us revisit.

- **Critic trigger cadence (10s default).** Revisit when a user reports either "too noisy" or "misses obvious moments."
- **Window size the Critic sees.** Revisit when `critic_v0.md` is written.
- **Mentorship dialogue UI details.** Revisit at v0.5 — the protocol message type is reserved now so the framework doesn't need changes.
- **File-watching upgrade (polling → watchdog).** Revisit when polling latency becomes noticeable (>5s felt).
- **Non-Claude-Code Researchers.** Revisit when a Scientist wants to use Cursor / Aider / etc.
- **Multiple concurrent Researcher sessions.** Revisit when a Scientist runs parallel experiments needing separate Critics.
- **Remote / GPU-split deployment.** Revisit if a user moves the Researcher to a separate host (e.g., a Linux GPU box with the Scientist on a laptop).

---

## Implementation sequence (v0, ~11 days)

Rough order. Not a Gantt — just dependencies.

1. **Repo skeleton + package layout** — `hunch/` Python package, `hunch watch` CLI entry point, `hunch init` stub.
2. **Replay buffer writer (Capture)** — poll CC transcript, chunk via existing parser, snapshot artifacts.
3. **Replay buffer schema + fixtures** — pin the JSONL shapes, write unit tests.
4. **Critic protocol + in-process stub** — stateless Sonnet call with mining-prompt port.
5. **Trigger + process lifecycle** — launch Critic, send ticks, timeouts, skip-if-busy.
6. **`hunches.jsonl` + `feedback.jsonl` writers** — append semantics, status-event helpers, status-folding reader.
7. **Side panel TUI** — tail hunches, render, capture keys (textual or prompt_toolkit).
8. **Tmux config + keybindings** — `Alt-g/b/s/i`, pane layout.
9. **UserPromptSubmit hook script** — reads hunches, prepends, appends status-change event, logs implicit feedback.
10. **`hunch init`** — scaffolds CLAUDE.md, layout, tmux snippet, config.
11. **End-to-end dry run** on Ariel's machine.
12. **Deliver to first user(s) + onboarding session.**

Depends on the Critic v0 being *implementable* (not polished) before step 11 — but its design is separately tracked.

---

## Open framework questions (post-review)

Surfaced during the 2026-04-14 adversarial review and downstream design conversation. Listed here rather than silently letting them rot in review notes.

**Q1: Autonomous-interject path.** The `UserPromptSubmit` hook fires only when the Scientist types. If the Researcher enters a long autonomous stretch (common pattern: debug → run → analyze → fix → rerun), hunches raised during that stretch queue and become post-mortem. The meeting-room framing assumes the Critic can interrupt at a natural pause; with no Scientist present, there is no natural pause.

Possible paths (none decided):

- `PreToolUse` hook — offers mid-stretch injection but risks breaking active workflow.
- `Stop` hook with `decision: block` — the Researcher can't end its turn while hunches are unread. Forces surfacing but frustrates if the Scientist is genuinely away.
- Confidence-threshold gating — high-bar hunches interject, low-bar ones queue. Requires the calibrated confidence we explicitly deferred in `critic_v0.md`.
- SDK wrapping for true mid-turn injection — heavy, but the only path to real-time interjection.

Strategic stance: v0 accepts post-mortem for the Scientist-away case (since the Scientist's next prompt is itself a natural pause). This is the top v0.5 candidate. The transition from "Scientist-gated" to "autonomous interject" is almost certainly per-hunch (a confidence threshold), not a global mode flip.

**Q2: Hunch queue UX.** Corollary of Q1. If hunches pile up during a Scientist-away stretch, the side panel has to support triage on return rather than overwhelm. A wall of 15 hunches is worse than zero — the Scientist bounces.

v0 minimum viable: chronological list with one-line smells; Scientist skims and acts per hunch. Out of scope for v0, likely needed by early v0.5:

- Staleness flag — "is this hunch still live?" check against current replay state.
- Self-resolution detection — Critic notices on a later tick that a concern was addressed, marks the original suppressed without Scientist involvement.
- Topic grouping — multiple hunches about the same artifact collapsed in the UI.

**Q3: JSONL concurrency safety.** Invariant #4 says replay-buffer JSONLs are append-only and readers handle concurrent writes "gracefully." On modern Linux local filesystems (ext4, xfs, btrfs) the kernel serializes regular-file appends via inode locks; empirically, 8 writers × 100 × 16KB lines without any locking produced zero corrupt JSON. So on the target environment, plain `open("a")` + `write()` already holds the contract.

Fix is concentration, not correction. All four JSONL writers (`hunches.py`, `feedback.py`, and the two `_append_*` methods in `capture/writer.py`) now go through a single `append_json_line(path, entry)` helper that takes `fcntl.flock(LOCK_EX)` around the write. Two honest justifications:

1. **Concentration.** One place to strengthen guarantees if we ever run on a filesystem where the ext4 guarantee doesn't hold, or change format to something needing more than `O_APPEND` atomicity.
2. **Short-write retry.** Python's `BufferedWriter` will retry a partial `write(2)`; the lock keeps those retries contiguous.

Caveats documented in `hunch/journal/append.py`:

- Advisory `flock(LOCK_EX)` only blocks other lockful writers; a reader without `LOCK_SH` can still see torn lines on a filesystem without atomic writes. On ext4 this doesn't matter; current v0 readers don't take shared locks.
- NFS is not a target; `flock` semantics are mount-option dependent there. If it ever becomes a target, `fcntl.lockf(F_SETLK)` is the portable choice.
- The helper is about concurrency, not crash safety. `ENOSPC`, `SIGKILL`, or power loss can still leave a partial line.

Contract test (`tests/test_journal_concurrency.py`) verifies: valid JSON on every line, correct total count, and every `(worker, idx)` pair present exactly once.

---

## Appendix A: Replay buffer schemas

`.hunch/replay/conversation.jsonl` — one entry per parsed chunk:

```jsonc
{
  "tick_seq": 1234,
  "ts": "2026-04-14T10:23:11Z",
  "chunk_id": "c-0042",
  "chunk_type": "researcher_turn" | "scientist_turn" | "tool_result" | ...,
  "content": { /* parser-dependent */ }
}
```

`.hunch/replay/artifacts.jsonl` — one entry per artifact event:

```jsonc
{
  "tick_seq": 1235,
  "ts": "2026-04-14T10:23:14Z",
  "event": "write" | "edit" | "delete",
  "path": "writeups/exp_042.md",
  "snapshot_path": "artifacts/writeups_exp_042.md__20260414T102314Z",
  "diff": { /* for edits, old_string + new_string */ }
}
```

`.hunch/replay/hunches.jsonl` (event-sourced, strictly append-only):

```jsonc
// emit event (one per hunch, written by Critic)
{
  "type": "emit",
  "hunch_id": "h-0007",
  "ts": "2026-04-14T10:23:15Z",
  "emitted_by_tick": 87,
  "smell": "3× discrepancy between calibration runs",
  "description": "...",
  "triggering_refs": { "chunks": ["c-0040","c-0042"], "artifacts": ["writeups/exp_042.md"] }
}

// status-change event (0 or more per hunch, written by side panel or hook)
{
  "type": "status_change",
  "hunch_id": "h-0007",
  "ts": "2026-04-14T10:24:02Z",
  "new_status": "shown_to_researcher" | "suppressed" | "good_pending_inject" | ...,
  "by": "scientist_key:alt_g" | "hook:user_prompt_submit" | ...
}
```

Current state of a hunch is computed by folding events in timestamp order. The file is *strictly* append-only; readers are expected to handle concurrent writes (new events appearing while being read) gracefully.

`.hunch/replay/feedback.jsonl`:

```jsonc
{
  "ts": "2026-04-14T10:24:02Z",
  "hunch_id": "h-0007",
  "channel": "explicit" | "implicit",
  "label": "good" | "bad" | "skip" | "implicit",
  "scientist_reply": null | "<text of their prompt>"
}
```

## Appendix B: Critic protocol messages

See § 3 Critic (interface only) for the wire format. Reserved message types:

- `init`, `ready`
- `tick`, `tick_result`
- `mentorship_tick`, `mentorship_reply` *(reserved now, implemented v0.5+)*
- `shutdown`, `bye`

---

*This doc is a starting point, not a contract. Specific mechanisms will change as we build; the seven invariants and the component contracts are what we expect to be the durable claims of v0.*
