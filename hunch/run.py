"""Framework main loop: capture → trigger → critic → journal.

Wires the five pieces (parse, capture/writer, trigger, critic,
journal) together into the `hunch run` command. The loop is
deliberately simple — a single thread, synchronous, one iteration per
`poll_s`:

  1. Poll the active Claude Code transcript for new lines. Parse them
     into events.
  2. For each new event, write it to the replay buffer, then evaluate
     the TriggerV1 policy. If the trigger fires, call the Critic, get
     Hunches, write emit events to `hunches.jsonl`.
  3. Sleep briefly, repeat until interrupted.

Events are processed individually (not in batch) because TriggerV1 is
event-driven — it needs each event's type to detect turn boundaries
(user_text following assistant silence).

This module only owns the *wiring*. It doesn't know how to parse
transcripts (that's `parse/`), how to decide when to tick (`trigger`),
or how to produce hunches (`critic/`). Swapping any of those out
shouldn't require touching this file.

`RunConfig` captures the knobs that self-use will want to turn; the
CLI in `hunch/cli.py` translates argv into a `RunConfig`.
"""

from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.capture.writer import ReplayBufferWriter
from hunch.checkpoint import (
    CHECKPOINT_FILENAME,
    checkpoint_from_trigger_state,
    read_checkpoint,
    trigger_state_from_checkpoint,
    write_checkpoint,
)
from hunch.critic import Critic
from hunch.critic.stub import StubCritic
from hunch.filter import HunchFilter
from hunch.journal.hunches import HunchesWriter, read_current_hunches
from hunch.parse import ParserState
from hunch.parse.transcript import poll_new_events
from hunch.trigger import (
    TriggerV1Config,
    TriggerV1State,
    decide_tick_v1,
    mark_tick_finished_v1,
    mark_tick_started_v1,
    observe_event_v1,
)


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

    `trigger_config` controls when the Critic fires. Default is
    turn-end mode (fire when the Scientist speaks after Claude silence).
    """
    cwd: Path
    transcript_path: Path | None = None
    replay_dir: Path | None = None
    project_roots: list[str] = field(default_factory=list)
    poll_s: float = 1.0
    critic_factory: Callable[[], Critic] = StubCritic
    filter_enabled: bool = True
    anthropic_client: Any | None = None
    trigger_config: TriggerV1Config = field(
        default_factory=lambda: TriggerV1Config(min_debounce_s=300.0)
    )

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
    hunch_filter: HunchFilter = field(init=False)
    parser_state: ParserState = field(init=False)
    transcript_path: Path = field(init=False)
    trigger_state: TriggerV1State = field(init=False)
    log: Callable[[str], None] | None = None
    _stopped: bool = False
    _tick_counter: int = 0
    _hook_bookmark: int = 0
    _hunches_emitted: int = 0
    _checkpoint_path: Path | None = field(init=False, default=None)

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

        self.hunch_filter = HunchFilter(
            replay_dir=replay_dir,
            client=cfg.anthropic_client,
            enabled=cfg.filter_enabled,
            log=self.log,
        )
        existing = read_current_hunches(replay_dir / "hunches.jsonl")
        self.hunch_filter.init_from_existing(existing)

        self.trigger_state = TriggerV1State()
        self._hook_bookmark = self.writer.tick_seq

        self._checkpoint_path = replay_dir / CHECKPOINT_FILENAME
        cp = read_checkpoint(self._checkpoint_path)
        if cp is not None:
            self.parser_state = ParserState(
                line_offset=cp.parser_line_offset,
                project_roots=cfg.resolved_project_roots(),
            )
            # Derive tick_seq from the actual file rather than the
            # checkpoint value: if a crash happened between writing
            # events and checkpointing, the file has more lines than
            # the checkpoint knows about.  Counting is cheap and
            # prevents tick_seq collisions on resume.
            conv_path = replay_dir / "conversation.jsonl"
            if conv_path.exists():
                with open(conv_path) as f:
                    self.writer.tick_seq = sum(1 for _ in f)
            self.trigger_state = trigger_state_from_checkpoint(cp)
            self._tick_counter = cp.tick_counter
            self._hook_bookmark = max(cp.hook_bookmark, self.writer.tick_seq)
            self._hunches_emitted = cp.hunches_emitted
            if self.log is not None:
                self.log(
                    f"[resume] from checkpoint: "
                    f"parser_offset={cp.parser_line_offset} "
                    f"writer_seq={self.writer.tick_seq} "
                    f"ticks={cp.tick_counter}"
                )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def step_once(self) -> None:
        """One iteration: capture new events, evaluate trigger per event,
        then check for hook-injected claude_stopped events.

        Ingestion and evaluation are separated: all new events are
        written to the replay buffer first (fast, no critic calls),
        then the trigger is evaluated for each event.  This ensures
        the replay buffer is complete before any critic tick fires,
        so the critic always sees full context.
        """
        events, new_parser_state = poll_new_events(
            self.transcript_path, self.parser_state,
        )
        self.parser_state = new_parser_state

        if not events:
            self._check_hook_events()
            self._write_checkpoint()
            return

        project_roots = self.parser_state.project_roots
        tick_seq_before = self.writer.tick_seq

        # Phase 1: Write all events to the replay buffer (fast).
        bookmarks: list[int] = []
        for event in events:
            self.writer.append_events([event], project_roots)
            bookmarks.append(self.writer.tick_seq)

        # Phase 2: Evaluate trigger for each event (may call critic).
        for event, bookmark_now in zip(events, bookmarks):
            etype = event.get("type", "")

            now = time.monotonic()
            fire = decide_tick_v1(
                self.trigger_state,
                now,
                bookmark_now,
                etype,
                self.config.trigger_config,
            )

            if fire is not None:
                self._fire_tick(now, bookmark_now)

            self.trigger_state = observe_event_v1(
                self.trigger_state, etype, now,
            )

        delta = self.writer.tick_seq - tick_seq_before
        if delta > 0 and self.log is not None:
            self.log(
                f"[capture] +{delta} events "
                f"(tick_seq now {self.writer.tick_seq})"
            )

        self._check_hook_events()
        self._write_checkpoint()

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

    # ------------------------------------------------------------------
    # Hook-injected events
    # ------------------------------------------------------------------

    def _check_hook_events(self) -> None:
        """Scan conversation.jsonl for claude_stopped events appended by
        the Stop hook (tick_seq > writer.tick_seq).

        The Stop hook writes directly to conversation.jsonl, bypassing
        the writer. We detect these by reading tail lines with tick_seq
        beyond what the writer has assigned.
        """
        conversation_path = self.writer.conversation_path
        if not conversation_path.exists():
            return

        hook_events = _read_hook_events(
            conversation_path, self._hook_bookmark,
        )
        if not hook_events:
            return

        for entry in hook_events:
            tick_seq = entry["tick_seq"]
            etype = entry["type"]
            self._hook_bookmark = tick_seq

            if etype != "claude_stopped":
                continue

            now = time.monotonic()
            bookmark_now = tick_seq
            fire = decide_tick_v1(
                self.trigger_state,
                now,
                bookmark_now,
                etype,
                self.config.trigger_config,
            )

            if fire is not None:
                self._fire_tick(now, bookmark_now)

            self.trigger_state = observe_event_v1(
                self.trigger_state, etype, now,
            )

        if self.log is not None:
            self.log(
                f"[hook] +{len(hook_events)} hook event(s) "
                f"(hook_bookmark now {self._hook_bookmark})"
            )

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def _write_checkpoint(self) -> None:
        if self._checkpoint_path is None:
            return
        cp = checkpoint_from_trigger_state(
            self.trigger_state,
            ticks_fired=self._tick_counter,
            hunches_emitted=self._hunches_emitted,
            tick_counter=self._tick_counter,
            parser_line_offset=self.parser_state.line_offset,
            writer_tick_seq=self.writer.tick_seq,
            hook_bookmark=self._hook_bookmark,
        )
        write_checkpoint(self._checkpoint_path, cp)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fire_tick(self, now: float, bookmark_now: int) -> None:
        bookmark_prev = self.trigger_state.last_tick_bookmark
        self.trigger_state = mark_tick_started_v1(
            self.trigger_state, now, bookmark_now,
        )
        self._tick_counter += 1
        tick_id = f"t-{self._tick_counter:04d}"

        if self.log is not None:
            self.log(
                f"[tick {tick_id}] firing "
                f"(window {bookmark_prev}..{bookmark_now})"
            )

        t0 = time.monotonic()
        try:
            hunches = self.critic.tick(
                tick_id=tick_id,
                bookmark_prev=bookmark_prev,
                bookmark_now=bookmark_now,
            )
        finally:
            self.trigger_state = mark_tick_finished_v1(self.trigger_state)

        elapsed = time.monotonic() - t0
        ts = _utc_now_iso()
        if self.log is not None:
            self.log(
                f"[tick {tick_id}] {len(hunches)} hunch(es) emitted "
                f"({elapsed:.1f}s)"
            )
        hunch_ids = [self.hunches_writer.allocate_id() for _ in hunches]
        filter_results = self.hunch_filter.filter_batch(
            hunches, bookmark_prev, bookmark_now, hunch_ids=hunch_ids,
        )
        for fr, hid in zip(filter_results, hunch_ids):
            if fr.passed:
                self.hunches_writer.write_emit(
                    hunch=fr.hunch,
                    hunch_id=hid,
                    ts=ts,
                    emitted_by_tick=self._tick_counter,
                    bookmark_prev=bookmark_prev,
                    bookmark_now=bookmark_now,
                )
                self._hunches_emitted += 1
                if self.log is not None:
                    self.log(f"  - {hid} {fr.hunch.smell}")
            else:
                self.hunches_writer.write_filtered(
                    hunch=fr.hunch,
                    hunch_id=hid,
                    ts=ts,
                    emitted_by_tick=self._tick_counter,
                    bookmark_prev=bookmark_prev,
                    bookmark_now=bookmark_now,
                    filter_type=fr.filter_type,
                    filter_reason=fr.reason,
                    duplicate_of=fr.duplicate_of,
                )
                if self.log is not None:
                    self.log(
                        f"  - {hid} [filtered:{fr.filter_type}] "
                        f"{fr.hunch.smell}"
                    )

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # noqa: ARG001 — signal API
            self.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except ValueError:
                pass


def _read_hook_events(
    conversation_path: Path,
    after_tick_seq: int,
) -> list[dict]:
    """Read entries from conversation.jsonl with tick_seq > after_tick_seq.

    Only returns entries beyond what the runner has already processed.
    Reads from the end of the file for efficiency.
    """
    results = []
    try:
        with open(conversation_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk_size = min(8192, size)
            if chunk_size == 0:
                return []
            f.seek(size - chunk_size)
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            tick_seq = int(entry.get("tick_seq", 0))
            if tick_seq > after_tick_seq:
                results.append(entry)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return results


def _utc_now_iso() -> str:
    """Best-effort ISO-8601 UTC timestamp with 'Z' suffix."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
