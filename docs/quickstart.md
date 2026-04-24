# Quickstart — Running Hunch live

How to run the Hunch Critic alongside a Claude Code research session.

---

## Prerequisites

- Python 3.11+
- The `hunch` package installed (`pip install -e .` from this repo)
- An Anthropic API key (for the Sonnet-backed Critic)
- A running Claude Code session in your project directory

## Setup (once per project)

```bash
cd /path/to/your/project
hunch init
```

This does three things:
1. Creates `.hunch/replay/` — where the framework stores its event log.
2. Merges a `UserPromptSubmit` hook into `.claude/settings.local.json` — so pending hunches are injected into the Researcher's context when you type.
3. Merges a `Stop` hook into the same file — so a `claude_stopped` event is appended to the replay buffer when Claude finishes a turn, letting the Critic fire immediately (hunches are ready before you type).

Verify with `cat .claude/settings.local.json` — you should see both a `UserPromptSubmit` hook entry pointing to `hunch hook user-prompt-submit` and a `Stop` hook entry pointing to `hunch hook stop`.

## Running the Critic

Open a **second terminal** in the same project directory:

```bash
hunch run --critic sonnet
```

You should see output like:

```
hunch run: following /home/you/.claude/projects/-home-you-my-project/abc123.jsonl
           replay=.hunch/replay
           critic=SonnetCritic
           trigger=claude-stopped (debounce=300.0s)
           poll=1.0s
Ctrl-C to stop.
```

The framework is now tailing the Claude Code transcript. It will fire the Critic each time Claude finishes a turn (via the Stop hook). The debounce (default 5 minutes) prevents rapid-fire during back-and-forth exchanges — the colleague waits for a natural pause before raising a hand.

### What the logs mean

- **`[capture] +N events (tick_seq now M)`** — N new events parsed from the transcript. The framework is seeing your conversation.
- **`[hook] +N hook event(s)`** — N events from the Stop hook were detected. The Critic may fire if debounce has elapsed.
- **`[tick t-XXXX] firing (window A..B)`** — the trigger fired. The Critic is being invoked with events A through B.
- **`[tick t-XXXX] N hunch(es) emitted (3.2s)`** — the Critic returned. N hunches were emitted (0 is normal — most ticks produce nothing). The time shown is how long the model call took.
- **`  - h-XXXX some smell description`** — a hunch was emitted. It will appear in the side panel and be injected into the Researcher's next prompt.

### Dry run (no API calls)

To verify the framework wiring without spending API credits:

```bash
hunch run --critic sonnet-dry
```

This runs the full pipeline but skips the model call. You'll see ticks firing and prompt sizes logged, but no hunches emitted.

## Reviewing hunches

### Side panel (TUI)

In a third terminal:

```bash
hunch panel --replay-dir .hunch/replay
```

This shows a live-updating list of hunches with their statuses. Keyboard shortcuts for labeling (good/bad/skip).

### Command line

```bash
hunch list                              # show current hunches
hunch label h-0003 good                 # mark a hunch
```

### Web annotation UI (for deeper review)

```bash
hunch annotate-web --replay-dir .hunch/replay --run-dir .hunch/replay
```

Opens a browser UI with the full conversation, clickable artifact/figure refs, and annotation controls.

## How hunches reach the Researcher

When you type your next message in Claude Code, the `UserPromptSubmit` hook fires. It reads `hunches.jsonl`, finds any pending hunches, and prepends them as `additionalContext` to your message. The Researcher sees something like:

```
[Critic hunch, 2 min ago]
The calibration results in exp_042.md show a 3x discrepancy...
[/Critic hunch]
```

You don't need to do anything — it happens automatically. If you've labeled a hunch as "bad" or "skip" in the panel, it won't be injected.

## Useful flags

| Flag | Default | What it does |
|------|---------|--------------|
| `--critic sonnet` | `stub` | Use the accumulating Sonnet Critic (requires API key) |
| `--critic sonnet-dry` | — | Full pipeline, no model call (logs prompt sizes) |
| `--min-debounce-s N` | 300 | Minimum seconds between Critic ticks |
| `--poll N` | 1.0 | How often (seconds) to check for new transcript lines |
| `--transcript PATH` | auto | Explicit transcript path (default: auto-discover) |
| `--replay-dir PATH` | `.hunch/replay/` | Where the replay buffer lives |

## tmux layout (recommended)

For a comfortable setup, split your terminal into panes:

```
+----------------------------------+------------------+
|  Main: claude (your research)    |  hunch panel     |
|                                  |                  |
|                                  |                  |
+----------------------------------+------------------+
|  hunch run --critic sonnet                          |
+-------------------------------------------------+
```

Example tmux commands:

```bash
# Start in your project dir
tmux new-session -s research

# Pane 0: your Claude Code session
claude

# Split right for the panel
# Ctrl-b %
hunch panel

# Split bottom for hunch run
# Ctrl-b "  (from the main pane)
hunch run --critic sonnet
```

## Troubleshooting

**"no transcript found"** — Claude Code hasn't been started in this directory yet, or the transcript auto-discovery failed. Start a Claude session first, or pass `--transcript` explicitly.

**No ticks firing** — The trigger fires when Claude finishes a turn (via the Stop hook) and at least 5 minutes have passed since the last tick. If Claude hasn't finished a turn yet, no ticks will fire. For testing, use `--min-debounce-s 10` to fire more aggressively.

**Hunches not appearing in Claude's context** — Check that `hunch init` ran successfully and `.claude/settings.local.json` has both hook entries (UserPromptSubmit and Stop). The UserPromptSubmit hook injects hunches when you send a new message.

**Critic takes a long time** — The first tick is the slowest (no prompt cache). Subsequent ticks benefit from Anthropic's automatic prompt caching. Typical: 10-30s for the first tick, 3-8s after.

---

## Offline evaluation

You can run the Critic over a past research session to see what it would have flagged. This is useful for evaluating the Critic's quality, tuning trigger parameters, or reviewing a colleague's session after the fact.

### Running the offline Critic

There are two modes depending on what you have:

#### Case 1: You have a replay directory (from a prior `hunch run`)

If you ran `hunch run` in a project, the replay dir is at `<project>/.hunch/replay/`. It contains the parsed dialogue, artifact snapshots, and event logs.

```bash
hunch replay-offline \
  --replay-dir /path/to/project/.hunch/replay \
  --output-dir /path/to/project/.hunch/eval/run01 \
  --critic sonnet
```

The replay directory is **read-only** — the Critic reads conversation and artifacts from it but never writes to it. All output goes to `--output-dir`.

#### Case 2: You only have a raw Claude Code transcript

If you never ran `hunch run` but have the raw `.jsonl` transcript (from `~/.claude/projects/`), pass it via `--claude-log`. The command parses the transcript into `--replay-dir` first, then drives the Critic over it. The `--replay-dir` must be empty (or not exist yet) — it will be populated from the transcript.

```bash
hunch replay-offline \
  --replay-dir /path/to/fresh-replay \
  --output-dir /path/to/fresh-replay-eval/run01 \
  --claude-log ~/.claude/projects/-home-you-project/abc123.jsonl \
  --critic sonnet
```

#### Resuming a partial run

Both cases are **resumable**: if `--output-dir` already contains `checkpoint.json` from a partial run, the command resumes from where it left off — no duplicate API calls. If a Case 2 run was interrupted, resume it with the Case 1 command (the replay dir was already populated during the initial parse):

```bash
hunch replay-offline --replay-dir /path/to/.hunch/replay \
  --output-dir /path/to/eval/run01 --critic sonnet
```

To start fresh instead, delete the output directory:

```bash
rm -r /path/to/project/.hunch/eval/run01
```

### What to expect

The Critic fires at every turn boundary (when Claude finishes speaking and you respond), just like the live pipeline. Output looks like:

```
hunch replay-offline: from-dir .hunch/replay  critic=sonnet  ...  filter=on
  output → .hunch/eval/run01
[replay] t-0001 @ event 12 (bookmark 0→12) hunches=0
[replay] t-0002 @ event 38 (bookmark 12→38) hunches=1
  - h-0001 calibration drift
  [filter] already raised: calibration drift
  - h-0002 [filtered:novelty] calibration drift
[replay] done: events=412 ticks=15 hunches=8
```

Each tick takes 3-30s depending on context size and prompt cache. A 400-event session with ~15 ticks typically takes 2-5 minutes and costs ~$1-3 in API credits.

### Reviewing results

Hunches are written to `hunches.jsonl` inside the output directory.

**Quick list:**

```bash
hunch list --replay-dir /path/to/project/.hunch/eval/run01
```

**Web annotation UI** (full conversation + hunches side-by-side):

```bash
hunch annotate-web \
  --replay-dir /path/to/project/.hunch/replay \
  --run-dir /path/to/project/.hunch/eval/run01
```

Opens at `http://localhost:5555`. Click on a hunch to jump to the conversation context where it was raised.

### Useful flags

| Flag | Default | What it does |
|------|---------|--------------|
| `--critic sonnet` | `stub` | Use the Sonnet Critic (requires API key) |
| `--critic sonnet-dry` | — | Full pipeline, no model call (test wiring + trigger timing) |
| `--no-filter` | off | Disable dedup + novelty filter (emit all raw hunches) |
| `--min-debounce-s N` | 300 | Minimum seconds between ticks |
| `--max-events N` | all | Stop after N events (useful for smoke tests) |

### Dry run first

To verify everything is wired up before spending API credits:

```bash
hunch replay-offline \
  --replay-dir /path/to/project/.hunch/replay \
  --output-dir /tmp/hunch-dryrun \
  --critic sonnet-dry
```

This runs the full pipeline — parsing, trigger, accumulator, prompt assembly — but skips the model call. You'll see how many ticks fire and their bookmark windows, which tells you the Critic's cadence is working correctly.
