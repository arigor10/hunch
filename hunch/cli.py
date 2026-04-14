"""Hunch CLI — entry point for the `hunch` command.

v0 scope: this is a placeholder that prints a help message. Real
subcommands (`hunch init`, `hunch run`, `hunch status`) land once the
framework's trigger/surface/config components are wired up.

Advertised here (but not yet implemented) so users can see the intended
shape of the CLI when they run `hunch --help`.
"""

from __future__ import annotations

import sys

from hunch import __version__


_HELP = f"""\
hunch {__version__} — a meeting-room critic for agentic research

usage: hunch <command> [args]

commands (planned — v0 skeleton in progress):
  init     Scaffold .hunch/ config for a project
  run      Start the framework (capture + trigger + critic + surface)
  status   Print current replay-buffer / hunch counts

See docs/framework_v0.md for the architecture and roadmap.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(_HELP)
        return 0
    if argv[0] in ("-V", "--version"):
        sys.stdout.write(f"hunch {__version__}\n")
        return 0
    sys.stderr.write(f"hunch: '{argv[0]}' is not yet implemented (v0 skeleton).\n")
    sys.stderr.write("Run 'hunch --help' to see the planned command surface.\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
