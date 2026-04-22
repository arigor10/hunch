# Critic Roadmap

*Where the Critic is going, and why in this order.*

---

## Three generations

The Critic develops in three generations, each distinguished by a structural boundary.

**v0.x — single call, accumulating context.** The Critic sees a deliberately low-resolution view of the research session: the conversation between the Scientist and the Researcher, and the written artifacts they produce (experiment writeups, analysis docs, figures) — but *not* the code, raw data, terminal output, or tool invocations. This is the meeting-room view described in VISION.md: "not every keystroke, not every command, but the summarized flow of the work. Denser units, longer span." The bet is that this lower resolution buys a longer horizon — the Critic can hold hours of research context in a single prompt — and that the native capabilities of a frontier LLM, given a good prompt and this condensed view, are sufficient to catch the anomalies that matter. No tool use, no persistent state beyond the prompt itself. The accumulating design (see `critic_v0.1.md`) gives it long-horizon visibility within a single context window, and purge-and-rebuild when the window fills. v0 is what we ship first, because it is the simplest thing that can work end-to-end.

**v1.x — agentic, with tools and managed knowledge.** The Critic becomes an agent: it can read files, search, and — critically — maintain its own structured knowledge about the project. The transition from v0 to v1 is not just "add tools." It is the transition from *passive observation* to *active sense-making*: the Critic stops being a reader of a transcript and starts being a participant that builds and maintains a model of the research.

**v2.x — self-improving through mentorship.** The Critic learns from the Scientist. It detects its own gaps (hunches the Scientist caught that it missed), asks the Scientist how they knew, and distills the answer into transferable principles that improve future performance. This is the learning-by-mentorship loop described in VISION.md. v2 depends on v1: the mentorship loop is only as valuable as the knowledge substrate it writes into. A Critic that can't maintain a coherent mental model of the research (v1's knowledge base, hypothesis list, established facts) has nowhere useful to store what it learns from the Scientist. The principles would be appended to a broken knowledge base. v1 builds the substrate; v2 fills it through dialogue.

It is too early to detail v2. The rest of this document focuses on v1. For the accumulating Critic design see `critic_v0.1.md`.

---

## What makes v1 different

The framework invokes the Critic at regular intervals called *ticks* — moments where new research activity has accumulated since the last invocation (see `framework_v0.md` for the trigger policy). A single-call Critic, no matter how much context it accumulates, has a fundamental limitation: everything it knows must fit in the prompt, and its "understanding" resets to whatever the prompt contains on each tick. It has no working memory that it controls, no ability to structure its knowledge, and no way to notice that a particular fact is important enough to write down for later.

The agentic Critic changes this. It maintains external state between ticks — not just the transcript, but its own curated representation of what matters. This is the difference between a colleague who re-reads the meeting notes from scratch each time they're asked for input, and one who has been building a mental model of the project all along.

Two capabilities define the v1 generation:

### 1. Selective artifact reading

The v0 Critic sees artifact content inlined in the transcript — every write and edit, in chronological order. This is faithful but noisy: a long session might include dozens of artifact mutations, most irrelevant to the current tick. The v1 Critic sees *metadata* about artifact events (file path, timestamp, type of change) but reads the actual content only when it decides to. This mirrors the meeting-room framing: the colleague knows that an experiment results doc was just updated, and can pull it up if something about the current discussion warrants a closer look.

Tool access: `Read`, `Glob`, `Grep` — read-only. The Critic never modifies project files.

### 2. Project knowledge base

This is the most speculative and most interesting piece of v1.

The Critic maintains a structured knowledge base about the project — a living document that it updates as the research unfolds. Not a raw log of events (that's the transcript), but a curated, opinionated representation of project state. One concrete method we draw inspiration from is Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — an LLM-maintained wiki that the agent updates as it works and consults when it needs context.

The knowledge base is the Critic's own artifact, stored in the replay directory, updated by the Critic at its discretion. It is *not* the Scientist's notes, not the Researcher's docs, not an auto-generated summary. It is what the Critic thinks is important to remember, structured the way the Critic finds useful. The contents might include:

- **Established facts and constraints.** Baseline numbers, methodological requirements, known limitations of the experimental setup.
- **Known risks and recurring patterns.** Failure modes that have come up before and should be watched for.
- **Open questions.** Unresolved issues that may affect interpretation of future results.

Why this matters for hunches: the hardest noses to catch are the ones that require connecting a new result to something from many ticks ago. A v0 Critic must find the relevant prior evidence somewhere in a long transcript, hoping it wasn't purged. A v1 Critic with a knowledge base has already distilled the prior evidence into citable claims, and can immediately check whether a new result is consistent.

The act of maintaining a knowledge base is itself valuable for catching anomalies. As Karpathy notes in the LLM Wiki proposal, organizing and updating a structured document forces the agent to confront contradictions: when a new result conflicts with an existing entry, the Critic must either update the entry or flag the inconsistency. Both outcomes are useful — the first keeps the knowledge base accurate, the second is a hunch.

### 3. Hypothesis tracking

Building on the knowledge base, a natural next step is **explicit hypothesis tracking**: a section of the knowledge base that records the project's active hypotheses and their evidential status. Each time an experiment result comes in, the Critic checks it against the active hypotheses. If a result invalidates or strains a hypothesis, that's a hunch. This shifts the Critic from reactive ("does this specific result look odd?") to proactive ("does this result fit the project's accumulated picture?").

This is explicitly speculative. We don't know how well LLMs maintain structured knowledge documents over many updates. We don't know whether the knowledge base will drift, become stale, or accumulate errors that themselves cause false-positive hunches. We expect to learn these things by building it.

The knowledge base also lays the groundwork for v2's mentorship loop: principles learned from the Scientist need a structured, well-maintained knowledge substrate to live in. But in v1, the knowledge base is populated by the Critic's own observations, not by mentorship dialogue — that comes later.

---

## The progression within v1

Not all of v1 ships at once. The rough staging:

**v1.0 — agentic with read-only tools.** The Critic is an agent invoked via `claude` CLI with `--tools "Read,Glob,Grep"` and `--add-dir` pointing to the project snapshot. It receives the transcript (same accumulating format as v0) plus a file listing of available artifacts. It reads artifacts selectively. No wiki yet — just the shift from "everything inlined" to "metadata in transcript, content on demand."

**v1.1 — project knowledge base.** The Critic gains a knowledge base artifact that persists between ticks. At the start of each tick, the knowledge base is included in context. At the end, the Critic may update it. The knowledge base starts as a simple markdown document; structure emerges from the Critic's own organization, not from a schema we impose. Contradictions surfaced during updates are a natural source of hunches.

**v1.2 — hypothesis tracking.** The knowledge base gains an explicit hypothesis section. The Critic's per-tick prompt includes instructions to check new results against active hypotheses. This is a prompt-level change, not an architectural one — the knowledge base and the tools are already there.

Each step is independently deployable and evaluable. A v1.0 Critic that reads artifacts selectively but has no wiki is already more capable than v0. Each subsequent step adds a capability whose value can be measured against the same eval battery.

---

## What stays constant across generations

Some things don't change across generations:

- **The Critic protocol.** `init → tick → tick_result`. The framework doesn't know or care whether the Critic is a single call or an agent.
- **The output schema.** Smell, description, triggering_refs. A hunch is a hunch.
- **The trigger policy.** When the Critic fires is a framework concern, not a Critic concern.
- **The eval pipeline.** Labels, novelty, dedup, matching — all operate on hunches regardless of which Critic emitted them. A v1 Critic is evaluated on the same battery as v0.
- **The meeting-room framing.** The Critic is a colleague in the room, not a shoulder-watcher. It operates on dialogue and written artifacts, not on code or raw data.

This constancy is deliberate. It means we can run Critics from different generations on the same session and compare their hunches directly. It means eval infrastructure built for v0 carries forward. It means the Scientist's labeling effort is never wasted by a generation change.

---

## Open questions

- **Knowledge base format.** Free-form markdown? Structured sections? JSON? We lean toward letting the Critic organize it naturally and seeing what emerges, but there may be formats that degrade less over many updates.
- **Knowledge base size management.** The knowledge base will grow. When does it need its own purge/summarize cycle? Does the Critic self-manage this, or does the framework impose limits?
- **Hypothesis ontology.** How formal should the hypothesis list be? A loose list of prose claims? A structured table with columns for evidence, status, confidence? Too loose and the Critic can't efficiently check against it; too rigid and it becomes a form-filling exercise.
- **Cross-session persistence.** Does the knowledge base carry across research sessions? If yes, it becomes genuine long-term memory — powerful but potentially stale. If no, the Critic rebuilds it each session, which is safe but loses continuity.
---

*This document is a roadmap, not a spec. The v0 details are in `critic_v0.1.md`. The v1 details will get their own design docs as we build them. This document is the "why this order" and "where are we going" layer.*
