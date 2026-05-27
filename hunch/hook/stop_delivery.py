"""Async Stop hook — auto-delivers approved hunches without a user message.

Registered as an ``asyncRewake`` Stop hook. Runs in the background after
each Claude response, polling feedback.jsonl for newly-approved hunches.
When it finds any:

  1. Formats them as a ``<hunch-injection>`` block.
  2. Marks them ``surfaced`` in hunches.jsonl.
  3. Writes the block to stderr and exits with code 2.

Exit code 2 tells Claude Code to wake up and show the stderr content as
a system message — delivering the hunch without requiring the user to
type anything.

The next Claude response triggers another Stop event, which spawns a new
watcher instance. This creates a self-perpetuating delivery loop.

The UserPromptSubmit hook remains as a fallback for hunches approved in
the brief gap between watcher exit and new watcher spawn.

Concurrency: multiple watcher instances may be alive simultaneously
(one per Stop event within the max_wait window). An exclusive file lock
ensures only one watcher polls at a time; latecomers exit immediately.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import sys
import time
from pathlib import Path

from hunch.hook.user_prompt_submit import format_hunch_injection
from hunch.journal.feedback import read_hunch_edits, read_labeled_hunch_ids
from hunch.journal.hunches import HunchesWriter, read_current_hunches

POLL_INTERVAL_S = 5.0
MAX_WAIT_S = 3600.0
_LOCK_FILENAME = ".stop_delivery.lock"


def handle_stop_delivery(
    replay_dir: Path,
    poll_interval: float = POLL_INTERVAL_S,
    max_wait: float = MAX_WAIT_S,
) -> int:
    """Poll for approved hunches and deliver via asyncRewake.

    Returns 0 if ``max_wait`` expires without finding hunches, or if
    another watcher already holds the lock. A new watcher spawns on the
    next Stop event, so exiting early is safe.

    Returns 2 when hunches are delivered (triggers Claude rewake).

    Never raises — errors are logged to stderr and the process exits
    cleanly so it doesn't crash Claude Code.
    """
    lock_path = replay_dir / _LOCK_FILENAME
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            return 0

        try:
            return _poll_loop(replay_dir, poll_interval, max_wait)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
    except Exception as exc:
        print(f"[hunch stop-delivery] error: {exc}", file=sys.stderr)
        return 0


def _poll_loop(replay_dir: Path, poll_interval: float, max_wait: float) -> int:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            deliverable = _find_deliverable(replay_dir)
        except Exception as exc:
            print(f"[hunch stop-delivery] transient error: {exc}", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        if deliverable:
            _mark_surfaced(replay_dir, deliverable)
            edits = read_hunch_edits(replay_dir / "feedback.jsonl")
            injection = format_hunch_injection(deliverable, edits=edits)
            print(injection, file=sys.stderr)
            return 2

        time.sleep(poll_interval)
    return 0


def _find_deliverable(replay_dir: Path):
    hunches_path = replay_dir / "hunches.jsonl"
    if not hunches_path.exists():
        return []
    records = read_current_hunches(hunches_path)
    labels = read_labeled_hunch_ids(replay_dir / "feedback.jsonl")
    return [
        r for r in records
        if r.status == "pending" and labels.get(r.hunch_id) == "good"
    ]


def _mark_surfaced(replay_dir: Path, hunches) -> None:
    hunches_path = replay_dir / "hunches.jsonl"
    ts = _utc_now_iso()
    writer = HunchesWriter(hunches_path=hunches_path)
    for r in hunches:
        writer.write_status_change(
            hunch_id=r.hunch_id,
            new_status="surfaced",
            ts=ts,
            by="hook:stop_delivery",
        )


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    """Invoked as ``hunch hook stop-delivery``."""
    import argparse

    parser = argparse.ArgumentParser(prog="hunch hook stop-delivery")
    parser.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="replay-buffer directory (default: .hunch/replay/ under cwd)",
    )
    ns = parser.parse_args(argv)

    replay_dir = ns.replay_dir or (Path.cwd() / ".hunch" / "replay")
    return handle_stop_delivery(replay_dir)
