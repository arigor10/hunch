"""Framework main loop: capture → trigger → critic → journal.

v0 wires the five pieces (parse, capture/writer, trigger, critic,
journal) together into the `hunch run` command. The loop is
deliberately simple — a single thread, synchronous, one iteration per
`poll_s`:

  1. Poll the active Claude Code transcript for new lines. Parse them
     into events and append to the replay buffer.
  2. Ask the Trigger whether it's time to fire a tick. If yes, call the
     Critic, get a list of Hunches, write emit events to
     `hunches.jsonl`. If no, continue.
  3. Sleep briefly, repeat until interrupted.

This module only owns the *wiring*. It doesn't know how to parse
transcripts (that's `parse/`), how to decide when to tick (`trigger`),
or how to produce hunches (`critic/`). Swapping any of those out
shouldn't require touching this file.

`RunConfig` captures the knobs that self-use will want to turn; the
CLI in `hunch/cli.py` translates argv into a `RunConfig`.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.capture.writer import ReplayBufferWriter, poll_once
from hunch.critic import Critic
from hunch.critic.stub import StubCritic
from hunch.journal.hunches import HunchesWriter
from hunch.parse import ParserState
from hunch.trigger import TriggerLoop


# ---------------------------------------------------------------------------
# Transcript auto-discovery
# ---------------------------------------------------------------------------

def _project_dir_for_cwd(cwd: Path) -> Path:
    """Map a cwd to Claude Code's transcript-project-dir name.

    Claude Code stores transcripts under
    `~/.claude/projects/<encoded-cwd>/` where the encoding replaces
    path separators and underscores with dashes. Matches what
    `claude` itself does at session start.
    """
    encoded = str(cwd).replace("/", "-").replace("_", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded


def find_latest_transcript(cwd: Path) -> Path | None:
    """Return the most recently modified `.jsonl` under the cwd's
    Claude Code project dir, or None if the dir doesn't exist / is
    empty.
    """
    project_dir = _project_dir_for_cwd(cwd)
    if not project_dir.is_dir():
        return None
    jsonls = list(project_dir.glob("*.jsonl"))
    if not jsonls:
        return None
    return max(jsonls, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Config + runner
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    """Knobs for one `hunch run` invocation.

    `transcript_path` is the file the capture loop tails. If None at
    run time, `Runner.start` auto-discovers via `find_latest_transcript`.

    `replay_dir` is where the replay buffer (events + artifacts +
    hunches + feedback) lives. Default: `.hunch/replay/` under cwd.

    `project_roots` is the list of directories that are "the project"
    for path-normalization purposes — artifacts under these roots get
    stored as relative paths in the replay buffer. Default: [cwd].

    `critic_factory` lets callers inject a real Critic (Sonnet-backed,
    etc.) without changing this module. Default is `StubCritic` so
    the end-to-end pipeline can be exercised without an API key.
    """
    cwd: Path
    transcript_path: Path | None = None
    replay_dir: Path | None = None
    project_roots: list[str] = field(default_factory=list)
    interval_s: float = 10.0
    poll_s: float = 1.0
    critic_factory: Callable[[], Critic] = StubCritic

    def resolved_replay_dir(self) -> Path:
        return self.replay_dir or (self.cwd / ".hunch" / "replay")

    def resolved_project_roots(self) -> list[str]:
        if self.project_roots:
            return list(self.project_roots)
        return [str(self.cwd)]


@dataclass
class Runner:
    """Owns the framework loop for one run.

    Separated from `run_forever` so tests can drive single iterations
    via `step_once()` without touching sleep / signals.
    """
    config: RunConfig

    writer: ReplayBufferWriter = field(init=False)
    hunches_writer: HunchesWriter = field(init=False)
    critic: Critic = field(init=False)
    trigger_loop: TriggerLoop = field(init=False)
    parser_state: ParserState = field(init=False)
    transcript_path: Path = field(init=False)
    _stopped: bool = False
    _tick_counter: int = 0

    def __post_init__(self) -> None:
        cfg = self.config
        replay_dir = cfg.resolved_replay_dir()
        replay_dir.mkdir(parents=True, exist_ok=True)

        transcript = cfg.transcript_path or find_latest_transcript(cfg.cwd)
        if transcript is None:
            raise RuntimeError(
                f"no transcript found under {_project_dir_for_cwd(cfg.cwd)} "
                f"— start a Claude Code session in {cfg.cwd}, then rerun, "
                f"or pass --transcript explicitly"
            )
        self.transcript_path = Path(transcript)

        self.writer = ReplayBufferWriter(replay_dir=replay_dir)
        self.hunches_writer = HunchesWriter(hunches_path=replay_dir / "hunches.jsonl")
        self.parser_state = ParserState(project_roots=cfg.resolved_project_roots())

        self.critic = cfg.critic_factory()
        self.critic.init({"replay_dir": str(replay_dir)})

        self.trigger_loop = TriggerLoop(
            critic=self.critic,
            bookmark_fn=lambda: self.writer.tick_seq,
            on_tick_result=self._write_hunches,
            interval_s=cfg.interval_s,
            poll_s=cfg.poll_s,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def step_once(self) -> None:
        """One iteration: capture new events, maybe fire a tick."""
        self.parser_state = poll_once(
            self.transcript_path, self.writer, self.parser_state
        )
        self.trigger_loop.step()

    def run_forever(self) -> None:
        """Blocking loop until SIGINT/SIGTERM or `stop()`."""
        self._install_signal_handlers()
        try:
            while not self._stopped:
                self.step_once()
                if self._stopped:
                    break
                time.sleep(self.config.poll_s)
        finally:
            self.critic.shutdown()

    def stop(self) -> None:
        self._stopped = True
        self.trigger_loop.stop()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_hunches(self, hunches: list[Any]) -> None:
        self._tick_counter += 1
        ts = _utc_now_iso()
        for hunch in hunches:
            hid = self.hunches_writer.allocate_id()
            self.hunches_writer.write_emit(
                hunch=hunch,
                hunch_id=hid,
                ts=ts,
                emitted_by_tick=self._tick_counter,
            )

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # noqa: ARG001 — signal API
            self.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except ValueError:
                # Not on the main thread (e.g. under some test runners);
                # graceful-shutdown responsibility shifts to the caller.
                pass


def _utc_now_iso() -> str:
    """Best-effort ISO-8601 UTC timestamp with 'Z' suffix."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
