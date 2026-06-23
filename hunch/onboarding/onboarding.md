# Agentic-Research Onboarding

*You are an agent setting up — or retrofitting — a project for agentic research with the
Hunch tooling. Execute the steps below. The human will mostly talk to you and answer
questions; you do the work.*

## Operating principles

- **Orchestrate deterministic primitives; don't freehand.** Use the provided commands
  (`hunch init`, `hunch doctor`, file copies) and verify each step's result. Don't
  improvise things a command does reliably.
- **Non-destructive by default.** Never overwrite or delete the user's existing files.
  Add; don't clobber. Keep every change easy to reverse.
- **This may be someone else's repo.** If the project was cloned from another owner, keep
  your additions easy to isolate (a branch, or gitignored) and do **not** commit or push
  without the user's explicit say-so. Summarize what you changed at the end.
- **Isolate your changes on a branch.** Before modifying any files in an existing repo,
  create a setup branch (`git checkout -b hunch-setup`), so every change is easy to review
  and undo. (A brand-new project has nothing to protect yet.)
- **Direction is the one thing you cannot read off the repo.** Even a repo that already
  has a `CLAUDE.md` may have no stated research *goal* — e.g. a `claude init`-generated
  file describes the code, not the intent. Always interview for direction (Step 2).

## Step 1 — Survey what's here

Read the repo and build a picture: README, any existing `CLAUDE.md`, the code layout,
docs, and any decision records. Summarize back to the user what the project appears to be
and how it's organized. Note what's **present** (structure to bind to) and what's
**missing** (no stated goal, no results registry, etc.). This subsumes what `claude init`
produces — you don't need to run it separately.

## Step 2 — Interview for direction (always)

Ask, conversationally:
- **What are you researching?** The question or goal, in your words.
- **What would success look like?** What result would make this worth it?
- **Scope / non-goals** — what's explicitly out of scope.
- **Compute & budget** — local GPUs, API-only, or a cloud platform (a rented GPU host, a
  notebook service, a cluster)? Any cost ceiling? (Many projects need no GPUs — don't
  assume.) Note what they use, so you can help set it up or run on it later.
- **Autonomy** — how much should I run on my own before checking in? (Default: the
  autonomy contract in `research_conventions.md`.)
- **Output aspirations** — aiming for a write-up, a paper, or just exploring?

Capture the direction into `vision.md` (or the project's equivalent). This is the most
important artifact — it installs the objective you'll reason against in the gaps between
explicit instructions.

**Do not invent answers the user didn't give.** If they leave a slot unanswered (scope,
budget, autonomy, output, or a specific benchmark), say so explicitly — record it under an
"Open questions to confirm with the advisor" heading in `vision.md`, with at most a clearly
labelled *inferred* candidate, never confident filler. End the interview by telling the
user **which slots you left open**, so the gap is visible rather than silently guessed.

## Step 3 — Lay down the research-process layer

`hunch onboard` has already placed `research_conventions.md` in the project root (and the
procedure + template under `.hunch/onboarding/`). Confirm it's present, and ensure the
project's `CLAUDE.md` pulls it in with a single line:

```
@research_conventions.md
```

These are the portable, path-agnostic principles (role, autonomy, workflow, hygiene,
literature, coding standards). Don't inline them — include them, so they stay updatable.

## Step 4 — Create or augment CLAUDE.md (non-destructively)

- **No `CLAUDE.md` exists** → create one from `.hunch/onboarding/claude_md_template.md`: fill the slots from
  the interview (title, what-this-is, direction pointer, compute, autonomy), add the
  `@research_conventions.md` include, and record the path bindings (Step 5).
- **A `CLAUDE.md` exists** → treat it as material to build on, not a fixed frame. Keep its
  accurate descriptive content (a `claude init` map is useful), and use your judgment to
  weave in the useful structure from `.hunch/onboarding/claude_md_template.md`. At minimum add the missing
  **direction** (point to `vision.md`), the `@research_conventions.md` include, and the
  path bindings; beyond that, fold in as much of the template's shape as serves this
  project — how exactly to combine them is your call. The one hard rule: be
  non-destructive — never discard the user's own notes, and do the merge on the setup
  branch (per the operating principles) so it's easy to review and undo. If you find
  existing content that's factually *wrong* (e.g. a stale `claude init` map that no longer
  matches the code), you may correct it — but make the change visible (a brief dated note on
  what changed and why), never a silent rewrite, and only for claims that are actually
  false, not to impose your preferences.

## Step 5 — Bind principles to this project's structure

Record, in `CLAUDE.md`, where the conventions' abstract slots live *in this project*:
plan docs, results registry, decisions log, literature notes. **Prefer existing
conventions** — if the repo already uses numbered ADRs under `decisions/`, bind the
decisions-log principle to that rather than creating a parallel scheme. If a slot has no
home yet, create the recommended location. (If you find an existing convention that's
better than the kit's default, flag it — the kit improves by adopting good patterns.)

## Step 6 — Seed (or adopt) the docs skeleton

Ensure there's a home for: the vision/direction (Step 2), a plan, a results registry, and
a decisions log. **Prefer an existing equivalent over creating a parallel file** — if the
repo already records results in, say, `reports/` or scattered `*-FINDINGS.md` docs, bind to
those rather than dropping an empty `results_registry.md` beside them. Create a new home
only when there is genuinely no equivalent. If an existing home works but is scattered (no
single index), note that in `CLAUDE.md` as an open improvement rather than silently
restructuring the user's docs.

## Step 7 — Install the Hunch substrate

Run `hunch init` in the project. It creates the replay buffer, merges Hunch's hooks into
`.claude/settings.local.json` (additively — it may add more than one `Stop` entry; that's
expected), and appends `.hunch/` and `.claude/settings.local.json` to the repo's
`.gitignore` so they don't pollute it. Confirm the hooks landed and that `.hunch/` and the
settings file are ignored. Note for the user: the one change `init` makes to a *tracked*
file is that `.gitignore` edit — everything else it adds is git-ignored or local.

## Step 8 — Validate

Run `hunch doctor`. It checks the prerequisites and reports each as OK / WARN / FAIL —
`claude` CLI present, hooks wired, replay dir valid, gitignore isolation, API keys.
**Resolve every FAIL before declaring success.** WARN items are things `doctor` could not
verify automatically (e.g. whether the `claude` CLI is *authenticated*) — surface those to
the user to confirm rather than assuming they pass. Never claim a check passed that
`doctor` did not actually pass.

## Step 9 — Hand off

Summarize what you set up and what changed (a short diff overview). Then open the working
layout: **if you're running inside tmux** (check `$TMUX`), run `hunch start` yourself — it
adds the `hunch panel` and `hunch run` panes *beside your current pane*, so this session
keeps running untouched. **If not in tmux**, tell the user to run `hunch start` (it opens a
fresh tmux session with the research agent + both panes). Then they can begin a research
cycle. Remind them nothing was committed or pushed.

A word on permissions, offered gently: Claude Code will ask for approval before running
commands — that's a safety feature, not a glitch. Let the user know the prompts are
expected, and that if the case-by-case approvals start to wear on them they can reduce the
friction at any time — by allowlisting the commands they keep approving, or, once they're
comfortable, granting broader permission — in `.claude/settings.local.json`. Don't push,
and don't grant permissions on their behalf unless they explicitly ask you to; the point is
only that they shouldn't be surprised or worn down into giving up.
