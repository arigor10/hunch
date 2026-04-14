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
    try:
        runner = Runner(config=config)
    except RuntimeError as e:
        sys.stderr.write(f"hunch run: {e}\n")
        return 1
    sys.stdout.write(
        f"hunch run: following {runner.transcript_path}\n"
        f"           replay={config.resolved_replay_dir()}\n"
        f"           critic={type(runner.critic).__name__}\n"
        f"           interval={config.interval_s}s poll={config.poll_s}s\n"
        f"Ctrl-C to stop.\n"
    )
    sys.stdout.flush()
    runner.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.command == "run":
        return _cmd_run(ns)
    if ns.command == "hook":
        return _cmd_hook(ns)
    if ns.command in ("init", "status"):
        sys.stderr.write(
            f"hunch {ns.command}: not yet implemented (v0 skeleton).\n"
        )
        return 2
    parser.print_help()
    return 0


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
