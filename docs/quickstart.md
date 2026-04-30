# Quickstart

Hunch runs in two modes:

- **Live** — runs alongside an active Claude Code session, watching in real time and surfacing hunches as they happen. The Researcher sees them automatically in their next prompt.
- **Offline** — runs over a past session's transcript after the fact. Useful for evaluating the Critic, reviewing a colleague's session, or tuning parameters.

Both use the same Critic and trigger logic. This guide covers live mode first, then [offline](#offline-evaluation).

---

## Prerequisites

- Python 3.11+
- Claude Code (the CLI tool) installed and working
- A working Claude-Code-based research agent (the "Researcher"). If you don't have one yet: create a directory for your project, add a `CLAUDE.md` that describes the project and sets up the research-in-cycles workflow (see [example](example_claude_md.md)), and run `claude`.

## Install

```bash
git clone https://github.com/arigor10/hunch.git
cd hunch
pip install -e ".[openrouter]"
```

(The `openrouter` extra installs the OpenAI SDK needed for OpenRouter backends. Omit it if you only plan to use the Claude CLI backend.)

Verify: `hunch --help` should print the available subcommands.

## Setup (once per project)

```bash
cd /path/to/your/project
hunch init
```

This does three things:
1. Creates `.hunch/replay/` — the event log that drives both live and offline evaluation (see [framework architecture](framework_v0.md) for details).
2. Merges a `Stop` hook into `.claude/settings.local.json` — this will trigger the Critic once finishes a turn, after a brief silence.
3. Merges a `UserPromptSubmit` hook into the same file — so pending hunches are injected into the Researcher's context when you type.

Verify with `cat .claude/settings.local.json` — you should see both a `UserPromptSubmit` hook entry pointing to `hunch hook user-prompt-submit` and a `Stop` hook entry pointing to `hunch hook stop`.

# Running the Critic (live)

Open a **second terminal** in the same project directory:

```bash
hunch run --config configs/sonnet_claude_cli.toml
```

You should see output like:

```
hunch run: following /home/you/.claude/projects/-home-you-my-project/abc123.jsonl
           replay=.hunch/replay
           critic=claude_cli:claude-sonnet-4-5-20250929 (via sonnet_claude_cli.toml)
           trigger=claude-stopped (debounce=300.0s)
           poll=1.0s
Ctrl-C to stop.
```

The framework is now tailing the Claude Code transcript. It will fire the Critic each time Claude finishes a turn (via the Stop hook). The debounce (default 5 minutes) prevents rapid-fire during back-and-forth exchanges — the colleague waits for a natural pause before raising a hand.

### Choosing a model

The `--config` flag points to a TOML file that controls which model to use, how to call it, and caching/retry behavior. Available configs:

| Config | Model | Provider | API key needed |
|--------|-------|----------|----------------|
| `configs/sonnet_claude_cli.toml` | Claude Sonnet | Claude CLI | No (uses your subscription) |
| `configs/sonnet_anthropic_sdk.toml` | Claude Sonnet | Anthropic API | `ANTHROPIC_API_KEY` |
| `configs/sonnet_openrouter.toml` | Claude Sonnet | OpenRouter → Anthropic | `OPENROUTER_API_KEY` |
| `configs/deepseek_v4_openrouter.toml` | DeepSeek V4 Pro | OpenRouter → SiliconFlow | `OPENROUTER_API_KEY` |
| `configs/gemma4_31b_openrouter.toml` | Gemma 4 31B | OpenRouter → Parasail | `OPENROUTER_API_KEY` |
| `configs/gemini_pro_openrouter.toml` | Gemini 3.1 Pro Preview | OpenRouter → Google AI Studio | `OPENROUTER_API_KEY` |

For OpenRouter models, set your API key first (get one at [openrouter.ai/keys](https://openrouter.ai/keys)):

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
hunch run --config configs/deepseek_v4_openrouter.toml
```

You can write your own config for any model available on OpenRouter — see [Writing a custom config](#writing-a-custom-config).

> **Legacy shorthand:** `--critic sonnet` still works as a shorthand for the Claude CLI backend (equivalent to `--config configs/sonnet_claude_cli.toml`). We recommend `--config` for new setups.

### What the logs mean

- **`[capture] +N events (tick_seq now M)`** — N new events parsed from the transcript. The framework is seeing your conversation.
- **`[hook] +N hook event(s)`** — N events from the Stop hook were detected. The Critic may fire if debounce has elapsed.
- **`[tick t-XXXX] firing (window A..B)`** — the trigger fired. The Critic is being invoked with events A through B.
- **`[tick t-XXXX] N hunch(es) emitted (3.2s)`** — the Critic returned. N hunches were emitted (0 is normal — most ticks produce nothing). The time shown is how long the model call took.
- **`  - h-XXXX some smell description`** — a hunch was emitted. It will appear in the side panel and be injected into the Researcher's next prompt.


## Reviewing hunches

### Side panel (TUI)

In a third terminal:

```bash
hunch panel --replay-dir .hunch/replay
```

This shows a live-updating list of hunches with their statuses. Use keyboard shortcuts to label each hunch as good, bad, or skip. Only hunches labeled "good" are surfaced to the Researcher on your next message. "Bad" suppresses the hunch; "skip" leaves it unlabeled for later review.

Don't overthink the labels during a session — if you're unsure, it's often faster to just let the Researcher respond to the hunch than to evaluate it yourself. Although the labels feed into precision/recall evaluations of the Critic, you can always revisit them in [offline evaluation](#reviewing-results), through a much friendlier UI.

### Command line (in case you don't like `hunch panel`)

```bash
hunch list                              # show current hunches
hunch label h-0003 good                 # mark a hunch
```

## How hunches reach the Researcher

When you type your next message in Claude Code, the `UserPromptSubmit` hook fires. It reads `hunches.jsonl`, finds any pending hunches, and prepends them as `additionalContext` to your message. The Researcher sees something like:

```
[Critic hunch, 2 min ago]
The calibration results in exp_042.md show a 3x discrepancy...
[/Critic hunch]
```

You don't need to do anything — it happens automatically. If you've labeled a hunch as "bad" or "skip" in the panel, it won't be injected.

## tmux layout (recommended)

For a comfortable setup, split your terminal into panes:

```
+----------------------------------+------------------+
|  Main: claude (your research)    |  hunch panel     |
|                                  |                  |
|                                  |                  |
+----------------------------------+------------------+
|  hunch run --config configs/sonnet_claude_cli.toml  |
+-----------------------------------------------------+
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
hunch run --config configs/sonnet_claude_cli.toml
```

## Troubleshooting

**"no transcript found"** — Claude Code hasn't been started in this directory yet, or the transcript auto-discovery failed. Start a Claude session first, or pass `--transcript` explicitly.

**No ticks firing** — The trigger fires when Claude finishes a turn (via the Stop hook) and at least 5 minutes have passed since the last tick. If Claude hasn't finished a turn yet, no ticks will fire. For testing, use `--min-debounce-s 10` to fire more aggressively.

**Hunches not appearing in Claude's context** — Check that `hunch init` ran successfully and `.claude/settings.local.json` has both hook entries (UserPromptSubmit and Stop). The UserPromptSubmit hook injects hunches when you send a new message.

## Starting Hunch mid-session

If you start `hunch run` after a session is already underway, the framework captures all historical events into the replay buffer — but the Critic won't raise hunches for them. The trigger only fires on new `claude_stopped` events (when Claude finishes a future turn), so the first tick after startup covers only the delta since `hunch run` began.

To get Critic coverage of the full history, use a two-step approach:

**Step 1 — Run the Critic offline over the existing transcript:**

```bash
hunch replay-offline \
  --claude-log ~/.claude/projects/<encoded-project>/abc123.jsonl \
  --replay-dir .hunch/history-replay \
  --output-dir .hunch/history-eval \
  --critic sonnet
```

This parses the full transcript and fires the Critic at every past turn boundary. Historical hunches land in `.hunch/history-eval/`. Review them with the annotation UI:

```bash
hunch annotate-web \
  --replay-dir .hunch/history-replay \
  --run-dir .hunch/history-eval
```

**Step 2 — Start live monitoring for new turns:**

```bash
hunch run --critic sonnet
```

The live run uses its own fresh `.hunch/replay/` directory and picks up from the current state of the session. New hunches are injected into the Researcher's context as usual.

The two runs are independent: historical hunches are in `.hunch/history-eval/`, live hunches in `.hunch/replay/`. The `UserPromptSubmit` hook only injects from the live dir, so historical hunches won't surface automatically — review them in the annotation UI and act on any that are still relevant.

---

# Offline evaluation

You can run the Critic over a past research session to see what it would have flagged. This is useful for evaluating the Critic's quality, which allows iteratively improving it. 

### Running the offline Critic

There are two modes depending on what you have:

#### Case 1: You have a replay directory (from a prior [`hunch run`](#running-the-critic-live))

If you ran [`hunch run`](#running-the-critic-live) in a project, the replay dir is at `<project>/.hunch/replay/`. It contains the parsed dialogue, artifact snapshots, and event logs.

```bash
hunch replay-offline \
  --replay-dir /path/to/project/.hunch/replay \
  --output-dir /path/to/project/.hunch/eval/run01 \
  --config configs/sonnet_claude_cli.toml
```

Or with a different model:

```bash
hunch replay-offline \
  --replay-dir /path/to/project/.hunch/replay \
  --output-dir /path/to/project/.hunch/eval/deepseek_run01 \
  --config configs/deepseek_v4_openrouter.toml
```

The replay directory is **read-only** — the Critic reads conversation and artifacts from it but never writes to it. All output goes to `--output-dir`.

#### Case 2: You only have a raw Claude Code transcript

If you never ran `hunch run` but have the raw `.jsonl` transcript of your work with the Researcher (from `~/.claude/projects/`), pass it via `--claude-log`. The command parses the transcript into `--replay-dir` first, then drives the Critic over it. The `--replay-dir` must be empty (or not exist yet) — it will be populated from the transcript.

```bash
hunch replay-offline \
  --replay-dir /path/to/fresh-replay \
  --output-dir /path/to/fresh-replay-eval/run01 \
  --claude-log ~/.claude/projects/-home-you-project/abc123.jsonl \
  --config configs/sonnet_claude_cli.toml
```

#### Resuming a partial run

Both cases are **resumable**: if `--output-dir` already contains `checkpoint.json` from a partial run, the command resumes from where it left off — no duplicate API calls. If a Case 2 run was interrupted, resume it with the Case 1 command (the replay dir was already populated during the initial parse):

```bash
hunch replay-offline --replay-dir /path/to/.hunch/replay \
  --output-dir /path/to/eval/run01 \
  --config configs/sonnet_claude_cli.toml
```

To start fresh instead, delete the output directory:

```bash
rm -r /path/to/project/.hunch/eval/run01
```

### What to expect

The Critic fires at every turn boundary (when Claude finishes speaking, and you respond, if there was enough of a pause in between), just like the live pipeline. Output looks like:

```
hunch replay-offline: from-dir .hunch/replay  critic=openrouter:deepseek/deepseek-v4-pro (via deepseek_v4_openrouter.toml)  ...  filter=on
  output → .hunch/eval/run01
[replay] t-0001 @ event 12 (bookmark 0→12) hunches=0 (3.2s)
[replay] t-0002 @ event 38 (bookmark 12→38) hunches=1 (4.1s)
  - h-0001 calibration drift
  [filter] already raised: calibration drift
  - h-0002 [filtered:novelty] calibration drift
[replay] done: events=412 ticks=15 hunches=8 backward_ts=0 wall=182s
[stats] calls=15 failures=0 input_tokens=245,012 cached_tokens=218,440 (89.2% hit) output_tokens=1,203 cost=$0.014821
```

The `[stats]` line shows token usage, cache hit rate, and total cost (for OpenRouter backends). This helps you track spend and verify that prefix caching is working.

### Reviewing results

Hunches are written to `hunches.jsonl` inside the output directory.

**Quick list:**

```bash
hunch list --replay-dir /path/to/project/.hunch/eval/run01
```

**[Web annotation UI](eval_infrastructure.md#the-annotation-ui)** (full conversation + hunches side-by-side):

```bash
# Recommended: project mode (discovers all runs + bank)
hunch annotate-web --project-dir /path/to/project

# Single-run mode (legacy, no bank integration)
hunch annotate-web \
  --replay-dir /path/to/project/.hunch/replay \
  --run-dir /path/to/project/.hunch/eval/run01
```

Opens at `http://localhost:5555`. Click on a hunch to jump to the conversation context where it was raised. In project mode, you can switch between eval runs in the sidebar, and labels are read from / written to the bank.

---
## Appendix

### Useful flags (replay-offline)

| Flag | Default | What it does |
|------|---------|--------------|
| `--config PATH` | — | TOML config file for the model backend |
| `--no-filter` | off | Disable dedup + novelty filter (emit all raw hunches) |
| `--min-debounce-s N` | 300 | Minimum seconds between ticks |
| `--max-events N` | all | Stop after N events (useful for smoke tests) |
| `--min-tick-interval-s N` | from config | Rate limiter: minimum wall-clock seconds between ticks (overrides config) |

### Dry run first

To verify everything is wired up before spending API credits:

```bash
hunch replay-offline \
  --replay-dir /path/to/project/.hunch/replay \
  --output-dir /tmp/hunch-dryrun \
  --critic sonnet-dry
```

This runs the full pipeline — parsing, trigger, accumulator, prompt assembly — but skips the model call. You'll see how many ticks fire and their bookmark windows, which tells you the Critic's cadence is working correctly.

---

## Label bank

After running the Critic (live or offline), use the bank to consolidate hunches across runs into a single project-level store. The bank dedup-matches hunches via an LLM judge, so the same concern gets the same label everywhere.

### Typical workflow

```bash
# 1. Run the Critic (produces hunches in .hunch/eval/<run>/)
hunch replay-offline --replay-dir ... --output-dir .hunch/eval/run01 --config ...

# 2. Sync hunches into the bank (dedup-matches against existing entries)
hunch bank sync --project-dir /path/to/project --yes

# 3. Annotate (project mode — sees all runs + bank labels)
hunch annotate-web --project-dir /path/to/project
```

Step 2 discovers all eval runs under `.hunch/eval/`, ingests their hunches into `.hunch/bank/hunch_bank.jsonl`, and (with `--yes`) migrates any legacy `labels.jsonl` files. New hunches are matched against existing bank entries — duplicates link to the existing entry and inherit its label automatically.

Step 3 opens the annotation UI in bank mode. You can switch between runs in the sidebar. Labels you set are written to the bank and propagate to all linked hunches across runs. The conversation is shown alongside each hunch so you can judge in context.

### Key flags (bank sync)

| Flag | Default | What it does |
|------|---------|--------------|
| `--project-dir PATH` | cwd | Project root containing `.hunch/` |
| `--run NAME` | all | Sync only this eval run |
| `--yes` | off | Auto-migrate legacy `labels.jsonl` into bank |
| `--window-k N` | 5 | Dedup comparison window (±N hunches by bookmark) |
| `--model MODEL` | `claude-haiku-4-5-20251001` | Model for dedup judge |

The bank is append-only and event-sourced — see [`hunch_bank_design.md`](hunch_bank_design.md) for the full design.

---

## `hunch run` flags

| Flag | Default | What it does |
|------|---------|--------------|
| `--config PATH` | — | TOML config file for the model backend |
| `--min-debounce-s N` | 300 | Minimum seconds between Critic ticks |
| `--poll N` | 1.0 | How often (seconds) to check for new transcript lines |
| `--transcript PATH` | auto | Explicit transcript path (default: auto-discover) |
| `--replay-dir PATH` | `.hunch/replay/` | Where the replay buffer lives |

---

## When to use an API key?

By default, `configs/sonnet_claude_cli.toml` calls the Critic via `claude --print`, which uses your Claude subscription. This is the simplest setup — no API key, no billing configuration.

Reasons you might want a different config:

- **Other models** — DeepSeek V4 Pro, Gemma 4 31B, or any model on OpenRouter. Set `OPENROUTER_API_KEY` and use the appropriate config file.
- **Prompt caching** — the Anthropic API, and OpenRouter providers that support it, use prompt caching to reuse the stable prefix across ticks. This can reduce cost by ~90% on long sessions. The engine automatically splits the prompt into a cached prefix and a varying suffix. The end-of-run stats show cache hit rate so you can verify it's working.
- **Cost visibility** — OpenRouter-based runs report exact USD cost at the end.
- **Rate limits** — the Claude CLI subscription shares your hourly quota with your interactive Claude Code session. A separate API key gives the Critic its own quota.

There are three ways to run Sonnet specifically:
- `sonnet_claude_cli.toml` — uses `claude --print`, no API key, but shares your subscription quota and has a 180s rate limit.
- `sonnet_anthropic_sdk.toml` — uses the Anthropic Python SDK directly. Set `ANTHROPIC_API_KEY`. Explicit prompt caching, own quota.
- `sonnet_openrouter.toml` — routes through OpenRouter to Anthropic. Set `OPENROUTER_API_KEY`. Same pricing as direct API, with cost tracking and caching.

### Writing a custom config

Copy an existing config from `configs/` and adjust:

```toml
[backend]
type = "openrouter"          # or "claude_cli", "anthropic_sdk"
model = "your/model-id"

[backend.auth]
env_var = "OPENROUTER_API_KEY"

[backend.params]
max_tokens = 8192
temperature = 0.0
timeout_s = 600
max_retries = 5
initial_backoff_s = 5.0
require_cache = true          # abort on cache miss (after warmup)
cache_warmup_ticks = 2        # allow N uncached ticks before enforcing
provider_order = ["Provider"]  # pin to a specific OpenRouter provider

[engine]
low_watermark = 140000
high_watermark = 180000
max_consecutive_failures = 3
min_tick_interval_s = 60      # rate limiter (seconds between ticks)
```

The `provider_order` field routes to specific OpenRouter providers (e.g. SiliconFlow for DeepSeek caching, Parasail for Gemma caching). Set `require_cache = true` to catch misconfigured caching early — the Critic will abort if it gets a cache miss after the warmup period.

The `min_tick_interval_s` field is useful for providers that rate-limit aggressively (e.g. SiliconFlow). It ensures a minimum pause between ticks. The CLI `--min-tick-interval-s` flag overrides this value.
