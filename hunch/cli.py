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
        "--interval",
        type=float,
        default=10.0,
        help="minimum seconds between Critic ticks (default: 10)",
    )
    run.add_argument(
        "--poll",
        type=float,
        default=1.0,
        help="loop wake-up interval in seconds (default: 1)",
    )

    sub.add_parser("init", help="(planned) scaffold .hunch/ config")
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

    return p


def _cmd_run(ns: argparse.Namespace) -> int:
    # Deferred import so `hunch --help` and `hunch --version` don't
    # pay for pulling in the framework loop and its dependencies.
    from hunch.run import RunConfig, Runner

    cwd = Path.cwd()
    config = RunConfig(
        cwd=cwd,
        transcript_path=ns.transcript,
        replay_dir=ns.replay_dir,
        project_roots=list(ns.project_roots or []),
        interval_s=ns.interval,
        poll_s=ns.poll,
    )
    def _log(msg: str) -> None:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    try:
        runner = Runner(config=config, log=_log)
    except RuntimeError as e:
        sys.stderr.write(f"hunch run: {e}\n")
        return 1
    _log(f"hunch run: following {runner.transcript_path}")
    _log(f"           replay={config.resolved_replay_dir()}")
    _log(f"           critic={type(runner.critic).__name__}")
    _log(f"           interval={config.interval_s}s poll={config.poll_s}s")
    _log("Ctrl-C to stop.")
    runner.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.command == "run":
        return _cmd_run(ns)
    if ns.command == "hook":
        return _cmd_hook(ns)
    if ns.command == "list":
        return _cmd_list(ns)
    if ns.command == "label":
        return _cmd_label(ns)
    if ns.command == "panel":
        return _cmd_panel(ns)
    if ns.command in ("init", "status"):
        sys.stderr.write(
            f"hunch {ns.command}: not yet implemented (v0 skeleton).\n"
        )
        return 2
    parser.print_help()
    return 0


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


def _cmd_hook(ns: argparse.Namespace) -> int:
    if ns.hook_name == "user-prompt-submit":
        from hunch.hook.user_prompt_submit import main as ups_main
        argv = []
        if ns.replay_dir is not None:
            argv.extend(["--replay-dir", str(ns.replay_dir)])
        return ups_main(argv)
    sys.stderr.write(f"hunch hook: unknown hook '{ns.hook_name}'\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
