# Hunch

Catches the moments in your agentic research that don't quite add up — before your agent builds on them.

Hunch is an early-stage open source project exploring runtime critique for agentic research workflows — a "meeting-room colleague" that watches a research session unfold and surfaces hunches about things worth a second look.

## Documentation

- **[Quickstart](docs/quickstart.md)** — get Hunch running on your project in 5 minutes. Covers live mode, offline replay, the annotation UI, and the label bank.
- **[VISION.md](VISION.md)** — the framing, the bet, and the design decisions behind Hunch.
- **[Critic Roadmap](docs/critic_roadmap.md)** — the three-generation plan for the Critic (v0 → v1 → v2).
- **[Framework v0](docs/framework_v0.md)** — architecture of the capture + trigger + critic loop.
- **[Eval Infrastructure](docs/eval_infrastructure.md)** — the annotation UI, label bank, and the flywheel that turns labeled hunches into precision/recall metrics.
- **[Bank Design](docs/hunch_bank_design.md)** — technical design of the project-level hunch bank (dedup matching, label inheritance, event sourcing).

## Status

Pre-alpha. The framework and the Critic are being developed in parallel — the goal of v0 is the simplest end-to-end loop that a real scientist can use, not a polished system.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
