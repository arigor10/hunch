# Hunch — Vision

*A living document. Captures the framing, the bet, and the specific design decisions behind Hunch.*

---



## Why Hunch

Frontier AI agents can perform scientific research with remarkable competence. They generate ideas, formulate hypotheses, plan and execute experiments (at least in computational domains), analyze the results, and even write full papers. The list of things they cannot yet do well is shrinking month over month. And yet — as of this writing, in early 2026 — human-conducted research remains unmatched. The reasons are an active field of study, and today's answers will not be tomorrow's. But after the coding, experimentation, and analysis bottlenecks have largely collapsed in favor of agents, one bottleneck remains stubbornly human: **verifying that the results make sense.**

Not verification as in *correctness*. Not verification as in *the plan was followed*. Verification of the kind that years of bench experience teach a scientist to do reflexively — looking at a result and immediately sensing that something is off:

> *"This can't be true — it's implausible that the system would respond like this to a change in hyperparameter X."*
>
> *"This result implicitly invalidates the premise of why we started; we should go back to square one and reconsider."*
>
> *"This behavior is inconsistent with what a previous experiment implied — is it a bug, or a real contradiction, an anomaly, or actually a scientific discovery?"*

A seasoned scientist arrives at these questions almost before they can articulate them. The hunch comes first; the explanation, sometimes much later.

LLMs do not seem to have developed this skill yet, and there may be several reasons. Maybe it requires integrating facts and theories across a longer time horizon than current context windows comfortably hold. Maybe its fuzzy, somewhere-between-science-and-art nature makes it hard to capture in an RLVR setup. Maybe it is a skill that resists learning from data or pure exploration — and instead requires learning from *mentorship*, which is arguably the primary vehicle by which humans acquire it. We don't know yet, and we expect the process of finding out to be fascinating.

Hunch is our attempt to build an agent that learns this missing skill — the Critic.


## Why the Critic

- **It's easier:** Generating brilliant research ideas remains something of a magic skill — rare, hard to decompose, reserved to a few. Critiquing research is different. Most human scientists learn to do it well with enough experience: the instinct to look at a result and sense that something is off develops through years of exposure, mentorship, and accumulated pattern recognition. Critiquing is the more learnable side of the research loop, and that is why we start here.

- **It's checkable:** In our experience, once a hunch is articulated and a problem is flagged to an AI agent, the agent is often perfectly capable of running with it — often sharpening the concern more crisply than the human who raised it, after looking at the right pointers (prior experiments, prior discussions, prior knowledge). It can debug, validate, propose a hypothesis and test it, even refute the concern after carefully weighing the evidence. **The asymmetry is striking: the agent cannot reliably *raise* the hunch, but once the hunch is raised, it can do most of the rest.**

These are two faces of the same principle: the **generation–discrimination gap**. Discrimination is systematically more tractable than generation, and the gap works in our favor twice. First, in the choice of *what* to build: critiquing research is more learnable than generating it. Second, within the Critic's own task: the hard part is noticing, the easy part is checking. False alarms can be filtered by the agent's own judgment; real misses — hunches the Critic should have raised but didn't — are caught by the human, and that is where the learning loop begins.

This is the gap Hunch is built around. Not capability, but attention and methodology — and, eventually, a path for the Critic to learn the missing piece through mentorship, over time.


## The minimal team

Three roles make up the loop Hunch sits inside. One naming convention to flag upfront: we reserve *Researcher* for the AI agent and *Scientist* for the human. The split takes a moment to adjust to, but it mirrors Hunch's central claim — the AI does the research; the human supplies the taste.

- **The Scientist** — the human in the loop. Sets the research direction, holds the scientific taste, catches the hunches the Critic misses, and mentors the Critic over time.
- **The Researcher** — the AI agent that executes the research. Writes code, runs experiments, analyzes results, writes up findings. In today's workflows this role is typically played by an agentic coding tool such as Claude Code or Cursor.
- **The Critic** — Hunch itself. Watches the flow of the research — the conversation between the Scientist and the Researcher, the intermediate writeups, the analytical claims as they appear — raises hunches when something doesn't add up, and learns from the Scientist's corrections over time.

Hunch builds only the Critic. The Scientist is the human using Hunch; the Researcher is whichever agentic research tool they are already using. Hunch slots in alongside them.

This is a deliberately small team. A common pattern in agentic workflows is to create multi-agent setups — a Student, a Professor, a Reviewer, an Engineer, all interacting — and to let wisdom-of-crowds dynamics do the work. Giving each agent its own focus, point of view, and incentive creates a healthy dynamic. However, the per-role prompts are often thin: they describe a role ("you are a student", "you are a professor") in a paragraph or two, relying mostly on the *native* ability of the LLM to perform the role.

Hunch makes the opposite bet: **one role, deep scaffolding.** The reasoning:

- The scientific Critic is arguably the limiting factor in agentic research quality.
- Deeply scaffolding one high-impact role is arguably more valuable than — or at least complementary to — lightly scaffolding many.

Richer team configurations can come later. For now, we keep the team minimal so the focus stays on the Critic role. We allow one exception: a code-review sub-agent invoked after the Researcher writes code. That pattern has become near-standard in agentic coding workflows and adds negligible team complexity.


## A meeting-room colleague

Hunch operates on the **research process itself** — the conversation between the Scientist and the Researcher, the intermediate results, the moment-by-moment reasoning — not on finished products like papers or writeups. Existing AI research reviewers often operate on polished outputs, where the messy intermediate state has already been cleaned away. By that point, the moment where someone might have said "this looks off" has either been caught or quietly explained away. Only the process exposes it.

The earliest detectable moment is *during the work*. A Critic that flags an issue while the experiment is running can save hours of wasted compute. A reviewer who flags it after the paper is written is too late to prevent the waste — and may be too late to change the methodology. The cost of a missed hunch compounds: by the time a wrong turn becomes a published result or an irreversible decision, undoing it is expensive. The earlier you catch it, the cheaper.

We call the framing **a meeting-room colleague**. Imagine a thoughtful peer sitting in on a long research session, listening without interrupting, occasionally raising a hand to say *"hold on, can we pause on this for a second?"* That is the role Hunch is trying to fill. Not a gatekeeper. Not a code reviewer. Not a copilot. A colleague whose job is to catch the moments that don't quite add up — quietly, in real time.

This is a deliberate choice of paradigm. A Critic could sit elsewhere — for example, looking over the Scientist's shoulder and intervening on every command and every line of code. But shoulder-watching is something today's LLMs can do natively: the things that go wrong at that resolution are more local and short-context. The mistakes that *survive* the shoulder-watcher are different in kind. They show up only across hours or days of work: a result that contradicts an experiment from yesterday, an analysis that quietly drifts away from the original question, a hypothesis that no longer fits the accumulated evidence. They are visible only at long horizon.

Long horizon is exactly where attention is hardest to hold. To stretch the horizon, Hunch *lowers the resolution* of what it watches: not every keystroke, not every command, but the summarized flow of the work. Denser units, longer span. This mirrors what a meeting-room colleague actually does: they don't lean over your terminal; they show up when you're synthesizing, and they ask the question that requires having heard the last hour.



## Smell, don't diagnose

A Critic in the meeting-room sense has two possible jobs:

1. **Smell** — notice that something doesn't add up.
2. **Diagnose** — explain what's wrong and what to do about it.

Hunch commits to the first only. The Critic flags the moment (*"this number looks inconsistent with what was claimed earlier"*) and stops there. Often the Researcher diagnoses on its own once the hunch is on the table; the Scientist joins when needed. Either way, diagnosis is downstream of the flag — and a different skill entirely.

Why the split? Two reasons. One is strategic: noticing is the part LLMs lack natively — once a hunch is raised, the Researcher can usually take it from there. Hunch targets the missing piece, not what's already working. The other is practical: there are many possible diagnoses for any given anomaly, so grading a Critic against any one is either too strict or too loose, while smell is binary enough to grade. We expect some cases will be genuinely debatable; the bet is that enough are unambiguous that catching them would already bring real value.

## Learning by mentorship

How do humans actually learn this skill? Through apprenticeship. You work alongside someone more experienced, they shadow what you do, they notice when you miss something, and — crucially — you ask them how they knew. *"How did you know that grain was about to split?"* — and the master articulates the rule. Over time it becomes part of your own instinct.

Mentorship is so much how scientific taste has always been passed on that naming it feels almost tautological. And yet the standard story of "human gives feedback to model" usually means RLHF (binary preferences) or learning from demonstrations (watch the expert). The more direct mechanism — learner detects its own gap, asks the expert in natural language, expert articulates the principle, principle accumulates — sits in an underexplored corner of the machine-learning space. It was never really expressible before: pre-LLM paradigms had no way to hold open-ended dialogue between learner and expert in the first place. Now they do.

Hunch is designed around this mechanism:

- **Self-aware gap detection** — the Critic notices when *it* missed something the human caught.
- **Learner-driven elicitation** — the Critic asks the human *how they knew*, not the other way around.
- **Dialogue-based principle refinement** — the Critic and the human work back-and-forth to articulate a transferable rule, not just a one-off correction.
- **Principle accumulation** — the resulting rule is added to the Critic's working knowledge, not just used to update a single weight.

Each of the four elements above exists somewhere in prior work — active learning, learning from natural language feedback, inverse RL, ARIA, AHCE — but the specific *combination*, applied to *scientific taste transfer*, appears novel. This is our strongest bet: not because the mechanism is exotic, but because it is the way humans actually transfer this exact kind of expertise, and because it only becomes expressible now that models can hold open-ended dialogue. If the ambitious problem of teaching scientific taste has a tractable path, we bet it's here. We treat "learning by mentorship" as a hypothesis: something worth building, something whose details will change, something we expect to learn a lot from getting wrong.

## Mergeability

If many scientists each train their own Critic on their own taste, can the principles compose? Most learned-agent architectures say no — when one agent is better at X, the only path is copy-replace. Knowledge is entangled, non-decomposable.

Hunch is being designed so that learned principles are **self-contained, transferable, and composable**. A computational biologist's Critic learns *"if two measurements on the same sample disagree by 3×, flag it"* — and that principle can be dropped into an ML scientist's Critic without architectural conflict. Principles from different Critics can be pooled, contested, and refined by a community over time.

This is not yet a feature; it is a *constraint on architectural decisions*. We avoid choices that entangle learned knowledge into representations that cannot be merged.

## What Hunch is *not*

To be clear about scope:

- **Not a code reviewer.** Existing tools handle code review well; Hunch operates above it.
- **Not a gatekeeper.** Hunch raises hunches; the Scientist and the Researcher decide what to do with them.
- **Not autonomous research.** Hunch presupposes a human in the loop. The human has the nose; Hunch is trying to learn it.
- **Not a paper reviewer.** The point is to catch things during the process, when intervention is cheap.
- **Not a replacement for the Researcher.** Hunch *watches* the Researcher and the Scientist work together; it does not replace either.

## Where Hunch applies

Hunch is for computational research today — ML, computational biology, data-driven sciences. The reason: Hunch needs a meeting-room conversation to listen in on, and that requires a research domain where an AI Researcher can meaningfully drive the work. Fields whose bench or field work remains human (wet biology, experimental physics, robotics in the physical world) are in scope only for their computational phases. As agentic AI extends into new domains, Hunch's scope follows. The line isn't drawn on principle; it's where the capability frontier sits in early 2026.

## Status

Hunch is **pre-alpha**. The project grew out of first-hand experience with agentic research in March and April of 2026. The framework and the Critic itself are being developed in parallel — the goal of v0 is the simplest end-to-end loop that a real scientist can use, not a polished system. Expect rapid iteration, embarrassing rough edges, and frequent course corrections.

We are building in public from the start, and deliberately so. Overindexing on a single scientist's taste — or a single research domain — is one of the main risks to this project: a Critic tuned to one person's instincts in one field would be a mirror of one scientist, not a tool for scientific taste. Engaging multiple scientists and multiple domains early is how we hope to avoid that trap. Scientists who find the framing compelling, whether as potential contributors or as people willing to stress-test the ideas with us, are very much invited.

## See also

Companion documents (to be added as the project develops):

- **Related work** — how Hunch relates to AI Scientist, Agent Laboratory, ARIA, AHCE, Interactive Task Learning, and other nearby projects.
- **Design** — architecture and implementation details.

---

*This document is a starting point, not a contract. Specific mechanisms will change as we build; the framing and the bets above are what we expect to be the durable claims of the project.*
