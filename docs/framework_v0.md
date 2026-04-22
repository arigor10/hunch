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
  └────────────┘           ▼          │ ├─ feedback.jsonl         │
                                      │ └─ labels.jsonl           │
                    ┌────────────┐    └───────────────────────────┘
                    │  Critic    │        ▲           ▲
                    │  process   │ ───────┘ writes    │ reads/writes
                    └────────────┘                    │
                         ▲ reads                      │
                         └──────────────────┬─────────┘
                                     (same files, one source of truth)
```

**Seven load-bearing components:**

1. **Capture** — writes the replay buffer
2. **Trigger** — decides when the Critic evaluates
3. **Critic** (interface only, implementation deferred) — reads replay buffer, produces hunches
4. **Filter** — drops hunches that are duplicates of earlier hunches or that the Researcher already raised in conversation
5. **Surface** — shows hunches to the Scientist; prepends approved hunches to Researcher's next prompt
6. **Feedback** — records Scientist reactions (explicit keys + implicit reply)

Plus cross-cutting: **Config** (paths, scaffolding) and the **Replay buffer** (the central data artifact).

---

## Design invariants (doors we refuse to close)

These are the interface-level commitments we take to the bank. Everything in v0 is implementable without violating them, and every future extension we've sketched fits behind them.

1. **The replay buffer is the single source of truth for Critic input.** Anything the Critic reads must be in, or referenced from, `.hunch/replay/`. The Critic must never need to reach outside this directory for primary data.
2. **The Critic is a process, not a function.** Launched by the framework via a configured command; communicates over stdio JSON (or a Unix socket). Stateful and stateless implementations both fit.
3. **Each Critic tick carries `(full_snapshot_bookmark, delta_bookmark)`.** The Critic either re-reads full or reads only since-bookmark. The framework doesn't care which.
4. **All replay-buffer JSONL files are strictly append-only.** This includes `hunches.jsonl`: a hunch's lifecycle (pending → surfaced → suppressed, etc.) is represented as a sequence of *status-change event* entries appended by whoever changes status. Current state is computed by folding events. No in-place mutation anywhere; every consumer (side panel, hook, future agentic Critic, future analytics) reads the same files and gets a full audit trail for free.
5. **Surface is file-triggered, not call-triggered.** Anything that wants to display, inject, or react to a hunch reads `hunches.jsonl` and `feedback.jsonl`. This lets v0's side panel, v0.5's PreToolUse hook, and v1's Stop hook all coexist and all consume the same data.
6. **Two hooks wire the framework into Claude Code.** The `UserPromptSubmit` hook injects approved hunches into the Researcher's context. The `Stop` hook appends `claude_stopped` events to enable the trigger. Both are thin file readers/writers over the replay buffer. Swapping to `PreToolUse` or SDK injection later changes *when* prepending fires, not *what* is prepended.
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

Each Critic tick costs real money and attention, so we trigger sparingly. The Critic also cannot interject mid-turn — Claude Code doesn't support interruption — so there's no value in firing while Claude is actively working. And firing during a back-and-forth between the Scientist and the Researcher would be disruptive; the Scientist is steering, not waiting for outside input.

The natural moment is when Claude has finished a turn and a silence gap opens — the Researcher has paused, the Scientist hasn't spoken yet, and a colleague glancing up from a notebook would say "hey, before you move on…". The framework fires when a `claude_stopped` event appears in the replay buffer (appended by the `Stop` hook when Claude finishes a turn), provided the debounce interval (default 300s) has elapsed since the last tick. See [`docs/trigger_policy_v1.md`](trigger_policy_v1.md) for the quantitative analysis behind this policy.

For offline replay (eval runs on historical data), `claude_stopped` events are synthesized at speaker boundaries (assistant → user transitions) by `synthesize_claude_stopped()` in the replay loader, so the same trigger policy applies.

**Contract:**
- Tick is a JSON message on the Critic's stdin: `{"tick_id", "bookmark_prev", "bookmark_now"}`.
- The framework guarantees at most one in-flight tick per Critic process.
- Skip policy (`skip` vs `queue` vs `kill_and_restart`) is config.

**Future extensions:**
- Critic self-triggering: agentic Critic runs continuously, emits hunches when *it* decides. Framework downgrades to notification-only.
- Cadence-learning: trigger policy tunes itself based on hunch acceptance rates in different conversation phases.

### 3. Critic (interface only)

The Critic is a black box behind its protocol. Its internals are specified separately in [`critic_v0.1.md`](critic_v0.1.md) (the accumulating design that ships as the production Critic).

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

**Shipped implementations:**
- **Stub** (`hunch.critic.stub`) — emits nothing; for testing the framework loop.
- **Sonnet v0.1** (`hunch.critic.sonnet`) — accumulating Critic (see `critic_v0.1.md`). Builds a growing prompt with dialogue, artifacts, and prior hunches across ticks. Shells out to `claude --print`. Prompt caching on the stable prefix. Invoked as `--critic sonnet` in the CLI.
- **Sonnet dry-run** (`hunch.critic.sonnet` with `dry_run=True`) — full pipeline without the model call. Logs prompt sizes per tick for cost estimation and debugging. Invoked as `--critic sonnet-dry` in the CLI.

**Future extensions (doors left open):**
- Long-running agentic Critic: `claude -r <session-id> -p "<tick message>"` per tick, or SDK-hosted agent. Maintains its own scratchpad tree of principles, updated as it goes. Identical protocol on the wire.
- Fine-tuned model backend.
- Ensemble of Critics with principle-merging across them (see VISION § Mergeability).
- Mentorship dialogue driven by the second tick type already reserved above.

### 4. Filter (novelty + dedup)

After the Critic emits hunches and before they are written to `hunches.jsonl`, the framework runs a lightweight filter that suppresses two classes of noise:

- **Duplicate hunches.** The Critic sees overlapping replay windows across ticks and may re-emit the same concern in different words. The filter compares each new hunch against the *K* most recent prior hunches (from `hunches.jsonl`) by semantic similarity, where *K* is a configurable window (default TBD). Capping the comparison set avoids quadratic cost growth over long sessions. If a new hunch matches an existing one above a threshold, it is dropped.
- **Already-raised concerns.** The Researcher or Scientist may have already discussed the concern in conversation. The filter scans the dialogue in the replay buffer (up to the current bookmark) for prior mentions of the hunch's smell. If the concern was already on the table, the hunch adds no signal and is dropped.

Both checks use an LLM judge (a fast, cheap call — separate from the Critic itself). Hunches that pass the filter are written to `hunches.jsonl` as `pending`; filtered hunches are either silently dropped or written with a `filtered` status (TBD — keeping them aids debugging but adds noise to the file).

The same filter logic is used in offline eval (see [`docs/eval_infrastructure.md`](eval_infrastructure.md)), where it runs as a post-processing step over the full set of emitted hunches.

### 5. Surface

**v0:** A tmux layout with two panes:
- **Main pane** runs `claude` (the Scientist's Researcher session).
- **Side pane** runs `hunch panel`, a TUI that tails `hunches.jsonl`, renders each hunch with its id and smell description, and captures keyboard shortcuts.

Keyboard shortcuts (tmux no-prefix bindings, delivered via `bind-key -n`):
- `Alt-g` — mark latest unresolved hunch as *good*, will be prepended to Researcher on next natural turn.
- `Alt-b` — mark as *bad*, suppress prepend.
- `Alt-s` — mark as *skip*, suppress prepend.
- `Alt-i` — mark as *good and inject now* — writes label, then `tmux send-keys -t <researcher_pane> Enter` to trigger a turn with no extra Scientist input, causing the UserPromptSubmit hook to fire and prepend the hunch.

All four keybindings can fire regardless of which pane has focus — the Scientist's cursor never leaves the main terminal.

**Trigger delivery (v0):** A `Stop` hook configured in `.claude/settings.local.json` appends a `claude_stopped` event to `conversation.jsonl` when Claude finishes a turn. The `hunch run` loop detects this event and fires the Critic (subject to debounce). Hunches are written to `hunches.jsonl` as `pending`.

**Approval gate (v0):** The Scientist reviews hunches in the side panel and labels them `good`, `bad`, or `skip`. Only hunches labeled `good` are injected into the Researcher's context — this ensures the Scientist triages hunches before they reach the Researcher, and encourages building the label bank.

**Injection mechanism (v0):** A `UserPromptSubmit` hook (also in `.claude/settings.local.json`) reads `hunches.jsonl` and `feedback.jsonl`, finds entries whose folded status is `pending` AND whose latest explicit label in `feedback.jsonl` is `good`. It formats them as a `<hunch-injection>` block injected as `additionalContext` ahead of the Scientist's message, then marks those hunches as `surfaced` in `hunches.jsonl`.

**Contract:**
- Side panel reads `hunches.jsonl` (folding events to derive current status); writes to `feedback.jsonl` and appends status-change events to `hunches.jsonl` (e.g. `{type: "status_change", hunch_id, new_status: "suppressed", by: "scientist_key"}`).
- Stop hook appends `{"type": "claude_stopped", "tick_seq": N, "timestamp": ...}` to `conversation.jsonl` when Claude finishes a turn.
- UserPromptSubmit hook reads `hunches.jsonl` + `feedback.jsonl`, prepends hunches that are pending AND labeled `good`, and appends a status-change event (`new_status: "surfaced"`).
- Neither the side panel nor the hooks ever talk directly to the Critic — they only read/write files.

**Additional CLI subcommands (beyond the side panel):**
- `hunch list` — print current hunches with statuses (useful for scripting and quick inspection without launching the TUI).
- `hunch label <hunch_id> good|bad|skip` — record an explicit label for a hunch from the command line. Writes to `feedback.jsonl` (same effect as the side-panel keybinding; the approval gate in the UserPromptSubmit hook reads this file).
- `hunch replay-offline` — drive the Critic offline over a pre-parsed replay directory (or parse a raw Claude log on the fly). Used for evaluation, prompt iteration, and running the Critic against historical sessions.
- `hunch annotate-web` — browser-based annotation UI (local Flask server) for reviewing hunches with full conversation context, artifact rendering, and figure display. Supports novelty and dedup filtering.

**Future extensions (doors left open):**
- **Mid-turn injection** via a `PreToolUse` or `PostToolUse` hook added alongside UserPromptSubmit. Same file, different trigger point.
- **Guaranteed delivery** via `Stop` hook with `decision: block` — prevents the Researcher from ending its turn while there are unread hunches.
- **SDK-wrapped session** for true mid-tool injection. The Surface abstraction (file-driven) means only the injection layer changes.
- **Side-panel dialogue mode** (mentorship): `Alt-?` opens a text prompt, sends a `mentorship_tick` to the Critic, renders reply inline. Dialogue log goes to `.hunch/mentorship/<hunch_id>.jsonl`.
- Mouse / click support in the side panel TUI.
- Slack / web UI surfaces that consume the same files.

### 6. Feedback

**v0:** Explicit labels only:

- **Explicit** — side-panel keys or `hunch label` CLI write to `feedback.jsonl`:
  `{"hunch_id", "label": "good"|"bad"|"skip", "ts"}`.

Labels are append-only. No deletion. A hunch may accumulate multiple labels (e.g. relabeling from `skip` to `good`); last-write-wins per `hunch_id`.

The UserPromptSubmit hook reads `feedback.jsonl` to enforce the approval gate: only hunches labeled `good` are injected. This makes the label bank grow as a natural byproduct of using the system.

**Contract:**
- `feedback.jsonl` is append-only.
- The Critic reads `hunches.jsonl` (for prior hunch content and status) and `feedback.jsonl` (for labels) on each tick via `prior_hunches` context to avoid repeating suppressed hunches.

**Future extensions:**
- **Implicit feedback** — when a hunch is injected, record the Scientist's reply text as an implicit label (`{"label": "implicit", "scientist_reply": ...}`). Captures *how* the Scientist acted on the hunch, which is often richer than the explicit label.
- **Autonomous injection (removing the gate)** — once the Critic is well-calibrated, consider injecting all pending hunches without requiring an explicit `good` label. The gate is the right default while the Critic is young.
- **Mentorship dialogue** — full back-and-forth log per hunch, stored separately (`.hunch/mentorship/<hunch_id>.jsonl`) but conceptually a second feedback channel.
- **Principle extraction** — dialogues produce principles the Critic writes to its own scratchpad; those principles are mergeable/transferable (see VISION § Mergeability).
- **Retroactive feedback** — Scientist can, hours or days later, retroactively label a hunch that was silently ignored.

### 7. Config & scaffolding

**Three layers (per invariant #7):**

- **Required interface (Layer 1):** Hunch needs to know two paths — the Claude Code transcript location (auto-detected from CC conventions) and the artifact directories to watch. That's the minimum.
- **Auto-discovery (Layer 2):** If no `hunch.config.toml` is present, Hunch scans the working directory for `.md` files (respecting `.gitignore`) and common figure dirs (`figures/`, `figs/`, `images/`, `plots/`). Shows discovered paths to user on first run with a confirm-or-edit prompt.
- **`hunch init` scaffolding (Layer 3):** Three side effects: (1) creates `.hunch/replay/` (with `artifacts/` subdir), (2) merges a `UserPromptSubmit` hook into `.claude/settings.local.json` (for hunch injection), and (3) merges a `Stop` hook into the same file (for `claude_stopped` event delivery to the trigger). Designed to be non-destructive: if the settings file already exists, hook entries are merged rather than overwritten. Future iterations may add a starter `CLAUDE.md`, tmux config fragment, and a populated `hunch.config.toml`.

**Config file (`hunch.config.toml`) v0 schema:**

```toml
[paths]
transcript = "~/.claude/projects/<auto-detected>"
artifacts  = ["writeups/", "figures/"]
replay     = ".hunch/replay/"

[trigger]
min_debounce_s   = 300    # minimum seconds between ticks
silence_s        = 30     # classic mode: fire after this much silence
max_interval_s   = 600    # classic mode: forced fire ceiling

[critic]
command          = "python -m hunch.critic.sonnet"
tick_timeout     = 30    # seconds
on_busy          = "skip"

[surface]
tmux_pane_main   = "researcher"
tmux_pane_side   = "hunch"
```

**Tmux setup is independent of the research loop.** The pane layout, keybindings, and side-panel launcher are pure UI plumbing. They observe nothing from the Researcher's conventions and require no cooperation from the Scientist's CLAUDE.md or directory structure. The research loop is unaware of Hunch; Hunch observes it. (The two places framework wiring touches the Researcher side are the `UserPromptSubmit` and `Stop` hooks — configured in Claude Code's own settings, not in anything the research loop provides.)

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

**D7: Approval gate — only `good`-labeled hunches are injected.**
UX correctness: hunches must be explicitly approved by the Scientist before they reach the Researcher. The gate lives in the UserPromptSubmit hook, which checks both the folded hunch status (`pending`) and the explicit label in `feedback.jsonl` (`good`). This ensures the Scientist triages hunches in the side panel before injection, and builds the label bank as a natural byproduct. `bad` and `skip` labels suppress injection; unlabeled hunches stay pending.

**D8: Tmux cross-pane keybindings with `send-keys`.**
Lets the Scientist control the side panel (good/bad/skip/inject) without leaving the main terminal. `send-keys` is tmux-native and cross-OS. The `Alt-i` (inject-now) key uses `send-keys Enter` to trigger a turn on the Researcher with no additional input, leveraging the UserPromptSubmit hook to prepend the hunch — elegant reuse of the injection mechanism.

**D9: Scaffolding as carrot, not stick.**
Hunch is positioned in VISION as a listener that "slots in alongside" whatever Researcher the Scientist uses. Prescribing methodology contradicts that. Layered config (auto-discovery → small config → opinionated init) keeps zero-friction for users adopting the defaults while leaving the door open for Scientists with idiosyncratic workflows.

**D10: `prior_hunches` in every Critic tick input.**
Cheap, underrated. Lets even stateless Critics avoid repeating themselves and learn (weakly) from past feedback without session state.

---

## Deferred decisions (with revisit triggers)

Things we chose not to decide now, and what would make us revisit.

- **Critic trigger cadence (300s debounce default).** Revisit when a user reports either "too noisy" or "misses obvious moments." See `docs/trigger_policy_v1.md` for the analysis behind the current default.
- **Window size the Critic sees.** Revisit as accumulator parameters are tuned (see `critic_v0.1.md`).
- **Mentorship dialogue UI details.** Revisit at v0.5 — the protocol message type is reserved now so the framework doesn't need changes.
- **File-watching upgrade (polling → watchdog).** Revisit when polling latency becomes noticeable (>5s felt).
- **Non-Claude-Code Researchers.** Revisit when a Scientist wants to use Cursor / Aider / etc.
- **Multiple concurrent Researcher sessions.** Revisit when a Scientist runs parallel experiments needing separate Critics.
- **Remote / GPU-split deployment.** Revisit if a user moves the Researcher to a separate host (e.g., a Linux GPU box with the Scientist on a laptop).

---

## Implementation sequence (v0, ~11 days)

Rough order. Not a Gantt — just dependencies.

1. **Repo skeleton + package layout** — `hunch/` Python package, `hunch panel` CLI entry point, `hunch init`.
2. **Replay buffer writer (Capture)** — poll CC transcript, chunk via existing parser, snapshot artifacts.
3. **Replay buffer schema + fixtures** — pin the JSONL shapes, write unit tests.
4. **Critic protocol + in-process stub** — stateless Sonnet call with mining-prompt port.
5. **Trigger + process lifecycle** — launch Critic, send ticks, timeouts, skip-if-busy.
6. **`hunches.jsonl` + `feedback.jsonl` writers** — append semantics, status-event helpers, status-folding reader.
7. **Side panel TUI** — tail hunches, render, capture keys (textual or prompt_toolkit).
8. **Tmux config + keybindings** — `Alt-g/b/s/i`, pane layout.
9. **UserPromptSubmit + Stop hook scripts** — UserPromptSubmit reads hunches + feedback, prepends approved hunches, appends status-change event. Stop hook appends `claude_stopped` event to enable the trigger.
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
- Confidence-threshold gating — high-bar hunches interject, low-bar ones queue. Requires calibrated confidence (deferred).
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
  "snapshot": "writeups_exp_042.md__20260414T102314Z",
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
  "bookmark_prev": 412,
  "bookmark_now": 437,
  "smell": "3× discrepancy between calibration runs",
  "description": "...",
  "triggering_refs": { "chunks": ["c-0040","c-0042"], "artifacts": ["writeups/exp_042.md"] }
}

// status-change event (0 or more per hunch, written by side panel or hook)
{
  "type": "status_change",
  "hunch_id": "h-0007",
  "ts": "2026-04-14T10:24:02Z",
  "new_status": "surfaced" | "suppressed" | ...,
  "by": "scientist_key:alt_g" | "hook:user_prompt_submit" | ...
}
```

Current state of a hunch is computed by folding events in timestamp order. The file is *strictly* append-only; readers are expected to handle concurrent writes (new events appearing while being read) gracefully.

### Bookmark window semantics

`bookmark_prev` and `bookmark_now` are recorded on emit so offline evaluators can reconstruct exactly what the Critic was looking at — not the *whole* replay state it could access, but the **window that convinced the Trigger to fire this tick**.

- `bookmark_now` — the highest `tick_seq` the Critic had in view when it emitted. Fixes causality: anything with `tick_seq > bookmark_now` happened after the fact and must not be treated as evidence.
- `bookmark_prev` — the `tick_seq` the previous tick stopped at. The half-open range `(bookmark_prev, bookmark_now]` is the **freshly-arrived window** that pushed Trigger over its threshold.

The pair is a range rather than a single point because the frame "what's new vs what was already on the table" is load-bearing for downstream evaluators:

- **Novelty judging.** The judge scans all dialogue with `tick_seq ≤ bookmark_now` for already-raised concerns. The divider at `bookmark_prev` doesn't gate the search — anything said anywhere before `bookmark_now` invalidates strict novelty. The divider is reasoning aid: it lets the judge articulate *where* the match lies (prior-context redundancy vs same-window concurrence vs genuine first voice).
- **Human labeling.** Same divider, same purpose — it makes "novel vs redundant" legible at a glance, which is the hard part of the annotation task.

Offline evaluators that need this attribution live in the eval harness, not in the framework — see [`docs/eval_infrastructure.md`](eval_infrastructure.md) for the novelty-judge contract.

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

`.hunch/replay/labels.jsonl` — offline eval annotations (distinct from live `feedback.jsonl`):

```jsonc
{
  "ts": "2026-04-14T11:00:00Z",
  "hunch_id": "h-0007",
  "label": "tp" | "fp" | "skip",
  "category": null | "<optional category tag>",
  "note": null | "<optional free-text note>"
}
```

Live feedback captures the Scientist's in-the-moment reaction; labels capture deliberate offline evaluation for precision measurement. Both are append-only; for labels, last-write-wins per `hunch_id`.

## Appendix B: Critic protocol messages

See § 3 Critic (interface only) for the wire format. Reserved message types:

- `init`, `ready`
- `tick`, `tick_result`
- `mentorship_tick`, `mentorship_reply` *(reserved now, implemented v0.5+)*
- `shutdown`, `bye`

---

*This doc is a starting point, not a contract. Specific mechanisms will change as we build; the seven invariants and the component contracts are what we expect to be the durable claims of v0.*
