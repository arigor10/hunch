"""Hunch CLI — entry point for the `hunch` command.

v0 exposes `hunch run`, which starts the framework loop (capture +
trigger + critic + journal). `init` and `status` are still planned
placeholders.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hunch import __version__


_DESC = "hunch — a meeting-room critic for agentic research"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hunch", description=_DESC)
    p.add_argument("-V", "--version", action="version", version=f"hunch {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    run = sub.add_parser(
        "run",
        help="start the framework loop (capture + trigger + critic)",
    )
    run.add_argument(
        "--transcript",
        type=Path,
        default=None,
        help="Claude Code .jsonl transcript to follow "
        "(default: auto-discover latest in ~/.claude/projects/<cwd>/)",
    )
    run.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )
    run.add_argument(
        "--project-root",
        dest="project_roots",
        action="append",
        default=[],
        help="project root for artifact-path normalization "
        "(default: cwd; repeatable)",
    )
    run.add_argument(
        "--poll",
        type=float,
        default=1.0,
        help="loop wake-up interval in seconds (default: 1)",
    )
    run.add_argument(
        "--min-debounce-s",
        type=float,
        default=300.0,
        help="trigger: min seconds between ticks (default: 300)",
    )
    run.add_argument(
        "--no-filter",
        action="store_true",
        help="disable the post-critic novelty + dedup filter",
    )
    run.add_argument(
        "--critic",
        choices=("stub", "sonnet", "sonnet-dry"),
        default="stub",
        help="critic implementation (default: stub — emits nothing). "
        "sonnet = accumulating v0.1 (shells out to `claude --print`). "
        "sonnet-dry = v0.1 with no model call (logs prompt sizes only).",
    )
    run.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML config file for a model backend. When provided, "
        "overrides --critic (uses CriticEngine with the configured backend).",
    )

    ini = sub.add_parser(
        "init",
        help="create .hunch/replay/ and merge the UserPromptSubmit hook into "
        ".claude/settings.local.json",
    )
    ini.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="project directory to initialize (default: current working directory)",
    )

    sub.add_parser("status", help="(planned) print replay-buffer / hunch counts")

    ls = sub.add_parser("list", help="print current hunches with statuses")
    ls.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )
    ls.add_argument(
        "--all",
        action="store_true",
        help="include hunches already labeled / dismissed (default: hide)",
    )

    lbl = sub.add_parser(
        "label",
        help="record an explicit Scientist label for a hunch",
    )
    lbl.add_argument("hunch_id", help="id like h-0007")
    lbl.add_argument(
        "label",
        choices=("good", "bad", "skip"),
        help="good | bad | skip",
    )
    lbl.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )

    pnl = sub.add_parser(
        "panel",
        help="launch side-panel TUI for reviewing and labeling hunches",
    )
    pnl.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )
    pnl.add_argument(
        "--poll",
        type=float,
        default=1.0,
        help="seconds between replay-buffer refreshes (default: 1)",
    )

    rpo = sub.add_parser(
        "replay-offline",
        help="drive the Critic offline over a parsed replay dir (or parse one "
        "on the fly from a Claude log)",
    )
    rpo.add_argument(
        "--replay-dir",
        type=Path,
        required=True,
        help="replay-buffer dir (conversation.jsonl, artifacts/). "
        "Read-only — the Critic reads from here but never writes to it.",
    )
    rpo.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="directory for eval output (hunches.jsonl, checkpoint.json). "
        "Must differ from --replay-dir. Resumable: re-running against a "
        "partially-completed output dir continues from the last checkpoint.",
    )
    rpo.add_argument(
        "--claude-log",
        type=Path,
        default=None,
        help="optional raw Claude .jsonl to parse into --replay-dir before "
        "running the critic. If omitted, --replay-dir must already be "
        "populated (via `hunch run` or `scripts/parse_transcript.py`).",
    )
    rpo.add_argument(
        "--critic",
        choices=("stub", "sonnet", "sonnet-dry"),
        default="stub",
        help="critic implementation (default: stub). "
        "sonnet = accumulating v0.1. sonnet-dry = v0.1 with no model "
        "call.",
    )
    rpo.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML config file for a model backend. When provided, "
        "overrides --critic (uses CriticEngine with the configured backend).",
    )
    rpo.add_argument("--min-debounce-s", type=float, default=300.0)
    rpo.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="cap on events consumed (default: all)",
    )
    rpo.add_argument(
        "--no-filter",
        action="store_true",
        help="disable the post-critic novelty + dedup filter",
    )
    rpo.add_argument(
        "--min-tick-interval-s",
        type=float,
        default=0.0,
        help="minimum wall-clock seconds between ticks (rate limiter). "
        "If a tick finishes faster, the driver sleeps the remainder. "
        "Useful for staying within API quota limits.",
    )

    aweb = sub.add_parser(
        "annotate-web",
        help="browser-based annotation UI (local Flask server)",
    )
    aweb.add_argument(
        "--replay-dir",
        type=Path,
        required=True,
        help="replay-buffer directory (conversation.jsonl)",
    )
    aweb.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="critic run directory (hunches.jsonl, labels.jsonl)",
    )
    aweb.add_argument(
        "--novel-only",
        action="store_true",
        help="only show novel hunches (requires novelty_summary.json in run-dir)",
    )
    aweb.add_argument(
        "--dedup",
        action="store_true",
        help="exclude duplicate hunches (requires dedup/dedup_summary.json in run-dir)",
    )
    aweb.add_argument(
        "--port",
        type=int,
        default=5555,
        help="port for the local server (default: 5555)",
    )

    hook = sub.add_parser("hook", help="Claude Code hook handlers (internal)")
    hook_sub = hook.add_subparsers(dest="hook_name", metavar="<hook>")
    ups = hook_sub.add_parser(
        "user-prompt-submit",
        help="UserPromptSubmit hook — inject pending hunches into prompt context",
    )
    ups.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )
    stop_hook = hook_sub.add_parser(
        "stop",
        help="Stop hook — append claude_stopped event to conversation.jsonl",
    )
    stop_hook.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )

    return p


def _cmd_run(ns: argparse.Namespace) -> int:
    # Deferred import so `hunch --help` and `hunch --version` don't
    # pay for pulling in the framework loop and its dependencies.
    from hunch.run import RunConfig, Runner
    from hunch.trigger import TriggerV1Config

    cwd = Path.cwd()

    def _log(msg: str) -> None:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    trigger_cfg = TriggerV1Config(min_debounce_s=ns.min_debounce_s)

    critic_factory = _resolve_critic_factory(ns.critic, _log, config_path=ns.config)
    filter_enabled = not ns.no_filter
    client = _try_anthropic_client() if filter_enabled else None
    config = RunConfig(
        cwd=cwd,
        transcript_path=ns.transcript,
        replay_dir=ns.replay_dir,
        project_roots=list(ns.project_roots or []),
        poll_s=ns.poll,
        critic_factory=critic_factory,
        filter_enabled=filter_enabled,
        anthropic_client=client,
        trigger_config=trigger_cfg,
    )

    try:
        runner = Runner(config=config, log=_log)
    except RuntimeError as e:
        sys.stderr.write(f"hunch run: {e}\n")
        return 1
    _log(f"hunch run: following {runner.transcript_path}")
    _log(f"           replay={config.resolved_replay_dir()}")
    _log(f"           critic={type(runner.critic).__name__}")
    _log(f"           trigger=claude-stopped (debounce={trigger_cfg.min_debounce_s}s)")
    _log(f"           filter={'on' if config.filter_enabled else 'off'}")
    _log(f"           poll={config.poll_s}s")
    _log("Ctrl-C to stop.")
    runner.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.command == "run":
        return _cmd_run(ns)
    if ns.command == "replay-offline":
        return _cmd_replay_offline(ns)
    if ns.command == "hook":
        return _cmd_hook(ns)
    if ns.command == "list":
        return _cmd_list(ns)
    if ns.command == "label":
        return _cmd_label(ns)
    if ns.command == "panel":
        return _cmd_panel(ns)
    if ns.command == "annotate-web":
        return _cmd_annotate_web(ns)
    if ns.command == "init":
        return _cmd_init(ns)
    if ns.command == "status":
        sys.stderr.write(
            "hunch status: not yet implemented (v0 skeleton).\n"
        )
        return 2
    parser.print_help()
    return 0


def _cmd_init(ns: argparse.Namespace) -> int:
    from hunch.init import init_project

    cwd = (ns.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        sys.stderr.write(f"hunch init: {cwd} is not a directory\n")
        return 1
    try:
        result = init_project(cwd)
    except RuntimeError as e:
        sys.stderr.write(f"hunch init: {e}\n")
        return 1

    replay_dir = cwd / ".hunch" / "replay"
    settings_path = cwd / ".claude" / "settings.local.json"
    sys.stdout.write(f"hunch init: {cwd}\n")
    for line in result.as_lines(replay_dir, settings_path):
        sys.stdout.write(line + "\n")
    return 0


def _cmd_replay_offline(ns: argparse.Namespace) -> int:
    from hunch.filter import HunchFilter
    from hunch.replay import (
        run_replay_from_claude_log,
        run_replay_from_dir,
    )
    from hunch.trigger import TriggerV1Config

    def _log(msg: str) -> None:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    replay_dir = ns.replay_dir.resolve()
    output_dir = ns.output_dir.resolve()

    if replay_dir == output_dir:
        sys.stderr.write(
            "hunch replay-offline: --output-dir must differ from "
            "--replay-dir (the replay buffer is read-only).\n"
        )
        return 1

    filter_enabled = not ns.no_filter
    client = _try_anthropic_client() if filter_enabled else None
    hunch_filter = HunchFilter(
        replay_dir=replay_dir,
        client=client,
        enabled=filter_enabled,
        log=_log,
    )
    existing_hunches_path = output_dir / "hunches.jsonl"
    if existing_hunches_path.exists():
        from hunch.journal.hunches import read_current_hunches
        existing = read_current_hunches(existing_hunches_path)
        hunch_filter.init_from_existing(existing)

    critic_factory = _resolve_critic_factory(ns.critic, _log, config_path=ns.config)
    critic = critic_factory()
    trigger_cfg = TriggerV1Config(min_debounce_s=ns.min_debounce_s)
    try:
        if ns.claude_log is not None:
            rate_msg = f"  rate-limit={ns.min_tick_interval_s}s" if ns.min_tick_interval_s > 0 else ""
            _log(
                f"hunch replay-offline: parse {ns.claude_log} → "
                f"{replay_dir}  critic={ns.critic}"
                f"  debounce={ns.min_debounce_s}s"
                f"  filter={'on' if filter_enabled else 'off'}"
                f"{rate_msg}"
                f"\n  output → {output_dir}"
            )
            result = run_replay_from_claude_log(
                claude_log=ns.claude_log,
                replay_dir=replay_dir,
                critic=critic,
                trigger_config=trigger_cfg,
                on_log=_log,
                max_events=ns.max_events,
                hunch_filter=hunch_filter,
                output_dir=output_dir,
                min_tick_interval_s=ns.min_tick_interval_s,
            )
        else:
            rate_msg = f"  rate-limit={ns.min_tick_interval_s}s" if ns.min_tick_interval_s > 0 else ""
            _log(
                f"hunch replay-offline: from-dir {replay_dir}"
                f"  critic={ns.critic}  debounce={ns.min_debounce_s}s"
                f"  filter={'on' if filter_enabled else 'off'}"
                f"{rate_msg}"
                f"\n  output → {output_dir}"
            )
            result = run_replay_from_dir(
                replay_dir=replay_dir,
                critic=critic,
                trigger_config=trigger_cfg,
                on_log=_log,
                max_events=ns.max_events,
                hunch_filter=hunch_filter,
                output_dir=output_dir,
                min_tick_interval_s=ns.min_tick_interval_s,
            )
    except (RuntimeError, FileNotFoundError) as e:
        sys.stderr.write(f"hunch replay-offline: {e}\n")
        return 1
    _log(
        f"[replay] done: events={result.events_consumed} "
        f"ticks={result.ticks_fired} "
        f"hunches={result.hunches_emitted} "
        f"backward_ts={result.backward_ts_warnings}"
    )
    return 0


def _try_anthropic_client():
    """Try to create an Anthropic SDK client. Returns None on failure."""
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception:
        return None


def _resolve_critic_factory(name: str, log, config_path: Path | None = None):
    """Map a --critic name (or --config path) to a zero-arg factory."""
    if config_path is not None:
        from hunch.backend import load_backend, load_config
        from hunch.critic.engine import CriticEngine, CriticEngineConfig

        full = load_config(config_path)
        def _factory():
            backend = load_backend(full.backend, log=log)
            engine_config = CriticEngineConfig(
                prompt_path=Path(full.engine.prompt_path) if full.engine.prompt_path else None,
                low_watermark=full.engine.low_watermark,
                high_watermark=full.engine.high_watermark,
                max_consecutive_failures=full.engine.max_consecutive_failures,
            )
            return CriticEngine(backend=backend, config=engine_config, log=log)
        return _factory

    if name == "stub":
        from hunch.critic.stub import StubCritic
        return StubCritic
    if name == "sonnet":
        from hunch.critic.sonnet import SonnetCritic, SonnetCriticConfig
        return lambda: SonnetCritic(log=log)
    if name == "sonnet-dry":
        from hunch.critic.sonnet import SonnetCritic, SonnetCriticConfig
        return lambda: SonnetCritic(
            config=SonnetCriticConfig(dry_run=True), log=log,
        )
    raise ValueError(f"unknown --critic value: {name!r}")


def _resolved_replay_dir(explicit: Path | None) -> Path:
    return explicit or (Path.cwd() / ".hunch" / "replay")


def _cmd_list(ns: argparse.Namespace) -> int:
    from hunch.journal.hunches import read_current_hunches
    from hunch.journal.feedback import read_labeled_hunch_ids

    replay_dir = _resolved_replay_dir(ns.replay_dir)
    hunches_path = replay_dir / "hunches.jsonl"
    if not hunches_path.exists():
        sys.stdout.write(
            f"(no hunches yet — {hunches_path} does not exist)\n"
        )
        return 0

    records = read_current_hunches(hunches_path)
    if not records:
        sys.stdout.write("(no hunches emitted yet)\n")
        return 0

    labeled = read_labeled_hunch_ids(replay_dir / "feedback.jsonl")
    visible = records if ns.all else [r for r in records if r.hunch_id not in labeled]

    if not visible:
        sys.stdout.write(
            f"(all {len(records)} hunch(es) already labeled — pass --all to see)\n"
        )
        return 0

    for r in visible:
        label_marker = f" [{_label_for(r.hunch_id, labeled)}]" if r.hunch_id in labeled else ""
        sys.stdout.write(f"{r.hunch_id}  ({r.status}){label_marker}  {r.smell}\n")
        if r.description:
            for line in r.description.splitlines():
                sys.stdout.write(f"            {line}\n")
        sys.stdout.write("\n")
    return 0


def _label_for(hunch_id: str, labeled: dict[str, str]) -> str:
    return labeled.get(hunch_id, "")


def _cmd_label(ns: argparse.Namespace) -> int:
    from hunch.journal.feedback import FeedbackWriter
    from hunch.journal.hunches import read_current_hunches

    replay_dir = _resolved_replay_dir(ns.replay_dir)
    hunches_path = replay_dir / "hunches.jsonl"
    if not hunches_path.exists():
        sys.stderr.write(
            f"hunch label: {hunches_path} does not exist — no hunches to label\n"
        )
        return 1

    known_ids = {r.hunch_id for r in read_current_hunches(hunches_path)}
    if ns.hunch_id not in known_ids:
        sys.stderr.write(
            f"hunch label: unknown hunch id {ns.hunch_id!r} "
            f"(known: {', '.join(sorted(known_ids)) or '<none>'})\n"
        )
        return 1

    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    writer = FeedbackWriter(feedback_path=replay_dir / "feedback.jsonl")
    writer.write_explicit(hunch_id=ns.hunch_id, label=ns.label, ts=ts)
    sys.stdout.write(f"labeled {ns.hunch_id} as {ns.label}\n")
    return 0


def _cmd_panel(ns: argparse.Namespace) -> int:
    from hunch.panel import run as panel_run

    replay_dir = _resolved_replay_dir(ns.replay_dir)
    replay_dir.mkdir(parents=True, exist_ok=True)
    return panel_run(replay_dir=replay_dir, poll_s=ns.poll)


def _cmd_annotate_web(ns: argparse.Namespace) -> int:
    from hunch.annotate_web import run_server

    return run_server(
        replay_dir=ns.replay_dir,
        run_dir=ns.run_dir,
        novel_only=ns.novel_only,
        dedup=ns.dedup,
        port=ns.port,
    )


def _cmd_hook(ns: argparse.Namespace) -> int:
    if ns.hook_name == "user-prompt-submit":
        from hunch.hook.user_prompt_submit import main as ups_main
        argv = []
        if ns.replay_dir is not None:
            argv.extend(["--replay-dir", str(ns.replay_dir)])
        return ups_main(argv)
    if ns.hook_name == "stop":
        from hunch.hook.stop import main as stop_main
        argv = []
        if ns.replay_dir is not None:
            argv.extend(["--replay-dir", str(ns.replay_dir)])
        return stop_main(argv)
    sys.stderr.write(f"hunch hook: unknown hook '{ns.hook_name}'\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
