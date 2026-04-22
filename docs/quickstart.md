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
