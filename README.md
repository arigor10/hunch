# Hunch

Catches the moments in your agentic research that don't quite add up — before your agent builds on them.

Hunch is an early-stage open source project exploring runtime critique for agentic research workflows — a "meeting-room colleague" that watches a research session unfold and surfaces hunches about things worth a second look.

## Get started

**1. Install** — `pipx install hunch` (or `pip install -e .` from a clone).

**2. Set up your project — the agent does it for you:**

```bash
cd /path/to/your/project
hunch onboard
```

This launches Claude, which **interviews you about your research direction** and scaffolds everything — your `CLAUDE.md`, the research workflow, and Hunch's capture hooks — non-destructively (it works fine on an existing repo you cloned).

**3. Open your workspace:**

```bash
hunch start
```

Your research agent on the left; the critic's panel and live run stacked on the right. That's it — Hunch now watches your research and surfaces hunches as you work.

## Documentation

- **[User Guide](docs/guide.md)** — the full reference: live + offline modes, the annotation UI, the label bank, custom models, and troubleshooting.
- **[VISION.md](VISION.md)** — the framing, the bet, and the design decisions behind Hunch.
- **[Critic Roadmap](docs/critic_roadmap.md)** — the three-generation plan for the Critic (v0 → v1 → v2).
- **[Framework v0](docs/framework_v0.md)** — architecture of the capture + trigger + critic loop.
- **[Eval Infrastructure](docs/eval_infrastructure.md)** — the annotation UI, label bank, and the flywheel that turns labeled hunches into precision/recall metrics.
- **[Bank Design](docs/hunch_bank_design.md)** — technical design of the project-level hunch bank (dedup matching, label inheritance, event sourcing).

## Status

Pre-alpha. The framework and the Critic are being developed in parallel — the goal of v0 is the simplest end-to-end loop that a real scientist can use, not a polished system.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
