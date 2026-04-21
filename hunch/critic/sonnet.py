"""Sonnet-backed Accumulating Critic (v0.1).

Holds a `CriticPromptStream` across ticks. Events append to the
timeline; periodically the accumulator purges the front when the
prompt approaches the high watermark. Result: a prefix-stable prompt
that actually sees long-horizon context, instead of the 20-event
sliding window v0 shipped.

Protocol (init/tick/shutdown) matches `hunch.critic.protocol.Critic`,
so the same class drives both `hunch run` (live) and `hunch
replay-offline` (offline). Input is always the replay buffer — we
never look at the raw Claude log.

Per-tick flow:
  1. Read events from conversation.jsonl with tick_seq in
     (last_seen, bookmark_now]; append to the stream.
  2. Sync any new emit events from hunches.jsonl and new labels from
     feedback.jsonl into the stream (so prior hunches/labels appear in
     the next prompt).
  3. If the stream projects over the high watermark, purge.
  4. Render the prompt.
  5. If config.dry_run: log sizes, return []. Otherwise shell out to
     `claude --print`, parse the JSON array, return hunches to the
     framework (which assigns ids and writes hunches.jsonl).

The Critic never writes hunches.jsonl — that stays the framework's
responsibility (`HunchesWriter` in `hunch.journal.hunches`). On the
next tick we read back whatever ids the framework allocated, so the
rendered timeline matches disk.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.critic.accumulator import (
    CriticPromptStream,
    load_prompt_template,
)
from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.critic.stateless_sonnet import parse_response


DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


@dataclass(frozen=True)
class SonnetCriticConfig:
    """Runtime config for the v0.1 Sonnet Critic.

    Watermark defaults match the sim driver's settings that shipped
    hunches end-to-end on AR in the private repo. Lower them for
    tests or when using a smaller-context model.
    """
    model: str = DEFAULT_MODEL
    prompt_path: Path | None = None           # None → packaged nose_v1.md
    cli_timeout_s: float = 600.0
    low_watermark: int = 150_000
    high_watermark: int = 200_000
    dry_run: bool = False


CallableClient = Any


@dataclass
class SonnetCritic:
    """Accumulating Sonnet-backed Critic (critic v0.1).

    Construct fresh per session. Framework calls init() once, then
    tick() per trigger firing, then shutdown() on exit.

    ``client`` is an optional Anthropic SDK-shaped object; tests inject
    a fake, users with API credits can pass a real one. When None
    (default), we shell out to ``claude --print`` and piggyback on the
    caller's Claude Code session.
    """
    config: SonnetCriticConfig = field(default_factory=SonnetCriticConfig)
    client: CallableClient | None = None
    log: Callable[[str], None] | None = None

    _replay_dir: Path | None = field(default=None, init=False, repr=False)
    _stream: CriticPromptStream | None = field(default=None, init=False, repr=False)
    _conv_cursor: int = field(default=0, init=False, repr=False)
    _last_seq_fed: int = field(default=0, init=False, repr=False)
    _hunches_synced: set[str] = field(default_factory=set, init=False, repr=False)
    _labels_synced: set[tuple[str, str]] = field(
        default_factory=set, init=False, repr=False,
    )
    _initialized: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------
    # Critic protocol
    # ------------------------------------------------------------------

    def init(self, config: dict[str, Any]) -> None:
        if self._initialized:
            raise RuntimeError("SonnetCritic.init called twice")
        replay_dir = config.get("replay_dir")
        if not replay_dir:
            raise RuntimeError("SonnetCritic.init: 'replay_dir' missing from config")
        self._replay_dir = Path(replay_dir)

        prompt_path = self.config.prompt_path
        if prompt_path is None:
            prompt_path = Path(__file__).resolve().parent / "prompts" / "nose_v1.md"
        preamble, suffix = load_prompt_template(prompt_path)
        self._stream = CriticPromptStream(
            preamble=preamble,
            suffix=suffix,
            low_watermark=self.config.low_watermark,
            high_watermark=self.config.high_watermark,
        )

        self._conv_cursor = 0
        self._last_seq_fed = 0
        self._hunches_synced = set()
        self._labels_synced = set()
        self._initialized = True

    def tick(
        self,
        tick_id: str,
        bookmark_prev: int,
        bookmark_now: int,
    ) -> list[Hunch]:
        if not self._initialized or self._stream is None or self._replay_dir is None:
            raise RuntimeError("SonnetCritic.tick called before init")

        appended = self._feed_conversation(bookmark_now)
        hunches_seen = self._sync_hunches(bookmark_now)
        labels_seen = self._sync_labels(bookmark_now)

        purged = 0
        if self._stream.should_purge():
            purged = self._stream.purge()

        prompt = self._stream.render()
        projected = self._stream.projected_tokens()

        if self.config.dry_run:
            self._log(
                f"[dry] {tick_id} window={bookmark_prev}..{bookmark_now} "
                f"appended={appended} hunches_synced={hunches_seen} "
                f"labels_synced={labels_seen} purged={purged} "
                f"prompt_chars={len(prompt):,} proj_tokens={projected:,} "
                f"timeline_len={len(self._stream.timeline)}"
            )
            return []

        try:
            text, input_tokens = self._call_model(prompt)
        except Exception as e:  # pylint: disable=broad-except
            self._log(f"[critic] model call failed: {e}")
            return []

        if input_tokens is not None:
            self._stream.update_observed_tokens(input_tokens)
            self._log(
                f"[critic] {tick_id} prompt_chars={len(prompt):,} "
                f"input_tokens={input_tokens:,} "
                f"proj_tokens={self._stream.projected_tokens():,}"
            )

        hunches = parse_response(text)
        if not hunches:
            self._log("[critic] (no hunches this tick)")
        # Don't append emitted hunches to the stream here — the framework
        # will write them to hunches.jsonl with the authoritative id, and
        # we pick them up via _sync_hunches on the next tick.
        return hunches

    def shutdown(self) -> None:
        self._initialized = False

    # ------------------------------------------------------------------
    # Stream feeding
    # ------------------------------------------------------------------

    def _feed_conversation(self, up_to_seq: int) -> int:
        """Read conversation.jsonl from the current cursor; append any
        events with tick_seq in (last_seen, up_to_seq] to the stream.

        Uses a byte cursor across ticks so we don't re-parse the whole
        file each tick. Safe because conversation.jsonl is append-only.
        """
        assert self._stream is not None and self._replay_dir is not None
        cpath = self._replay_dir / "conversation.jsonl"
        if not cpath.exists():
            return 0
        count = 0
        with cpath.open("r") as f:
            f.seek(self._conv_cursor)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    # Partial line — don't advance cursor past it; we'll
                    # retry next tick when it's flushed.
                    break
                self._conv_cursor = line_start + len(line)
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seq = entry.get("tick_seq")
                if not isinstance(seq, int):
                    continue
                if seq > up_to_seq:
                    # Don't feed beyond bookmark_now; rewind cursor so
                    # we re-read this line on the next tick.
                    self._conv_cursor = line_start
                    break
                if seq <= self._last_seq_fed:
                    continue
                if self._append_event(entry):
                    count += 1
                self._last_seq_fed = seq
        return count

    def _append_event(self, entry: dict[str, Any]) -> bool:
        """Feed one conversation.jsonl row to the stream. Returns True if
        the row produced a timeline event (some types are silently skipped).
        """
        assert self._stream is not None and self._replay_dir is not None
        etype = entry.get("type")
        seq = entry["tick_seq"]

        if etype == "user_text":
            self._stream.append_chunk_text(
                tick_seq=seq, role="user", text=entry.get("text", "")
            )
            return True
        if etype == "assistant_text":
            self._stream.append_chunk_text(
                tick_seq=seq, role="assistant", text=entry.get("text", "")
            )
            return True
        if etype == "artifact_write":
            path = entry.get("path", "")
            if not path.endswith(".md"):
                return False
            snap = entry.get("snapshot")
            if not snap:
                return False
            content = self._read_snapshot(snap)
            self._stream.append_artifact_write(
                tick_seq=seq, path=path, content=content
            )
            return True
        if etype == "artifact_edit":
            path = entry.get("path", "")
            if not path.endswith(".md"):
                return False
            if entry.get("skipped_reason"):
                # edit_before_known_base or old_string_not_found — the
                # capture writer already decided not to snapshot. Nothing
                # useful to show the model.
                return False
            diff = entry.get("diff") or {}
            old_s = diff.get("old_string", "")
            new_s = diff.get("new_string", "")
            if not old_s and not new_s:
                return False
            self._stream.append_artifact_edit(
                tick_seq=seq, path=path,
                old_string=old_s, new_string=new_s,
            )
            return True
        # figure / tool_error / unknown → skip.
        return False

    def _read_snapshot(self, snapshot_name: str) -> str:
        assert self._replay_dir is not None
        snap_path = self._replay_dir / "artifacts" / snapshot_name
        if not snap_path.exists():
            return ""
        try:
            return snap_path.read_text()
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # Hunches / labels sync — pick up what the framework wrote
    # ------------------------------------------------------------------

    def _sync_hunches(self, up_to_seq: int) -> int:
        """Read hunches.jsonl; append emit events to the stream for any
        hunch we haven't seen. Ids come from the framework — we never
        invent them. Returns count appended.
        """
        assert self._stream is not None and self._replay_dir is not None
        hpath = self._replay_dir / "hunches.jsonl"
        if not hpath.exists():
            return 0
        count = 0
        with hpath.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "emit":
                    continue
                hid = entry.get("hunch_id")
                if not isinstance(hid, str) or hid in self._hunches_synced:
                    continue
                smell = entry.get("smell", "")
                description = entry.get("description", "")
                refs_raw = entry.get("triggering_refs") or {}
                # Anchor at bookmark_now of the emit. Falls back to
                # up_to_seq if legacy records lack the field.
                bn = entry.get("bookmark_now")
                anchor = bn if isinstance(bn, int) and bn > 0 else up_to_seq
                self._stream.append_hunch(
                    tick_seq=anchor,
                    hunch_id=hid,
                    hunch=Hunch(
                        smell=smell,
                        description=description,
                        triggering_refs=TriggeringRefs.from_dict(refs_raw),
                    ),
                )
                self._hunches_synced.add(hid)
                count += 1
        return count

    def _sync_labels(self, up_to_seq: int) -> int:
        """Read feedback.jsonl; append label events for any we haven't
        seen. Dedup key is (hunch_id, label) — one label per hunch in
        v0, but the key survives if that assumption loosens.
        """
        assert self._stream is not None and self._replay_dir is not None
        fpath = self._replay_dir / "feedback.jsonl"
        if not fpath.exists():
            return 0
        count = 0
        with fpath.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                hid = entry.get("hunch_id")
                label = entry.get("label")
                if not isinstance(hid, str) or label not in ("good", "bad", "skip"):
                    continue
                key = (hid, label)
                if key in self._labels_synced:
                    continue
                lseq = entry.get("tick_seq")
                anchor = lseq if isinstance(lseq, int) and lseq > 0 else up_to_seq
                self._stream.append_label(
                    tick_seq=anchor, hunch_id=hid, label=label,
                )
                self._labels_synced.add(key)
                count += 1
        return count

    # ------------------------------------------------------------------
    # Model call
    # ------------------------------------------------------------------

    def _call_model(self, prompt: str) -> tuple[str, int | None]:
        """Call the model. Returns (response_text, total_input_tokens).

        total_input_tokens is the sum of uncached + cache_read +
        cache_create tokens — the full prompt size as the model saw it.
        None if usage couldn't be extracted.
        """
        if self.client is None:
            return self._call_via_cli(prompt)
        return self._call_via_sdk(prompt)

    def _call_via_sdk(self, prompt: str) -> tuple[str, int | None]:
        assert self.client is not None
        response = self.client.messages.create(
            model=self.config.model,
            max_tokens=1024,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        content = getattr(response, "content", None) or []
        text = ""
        if content:
            first = content[0]
            t = getattr(first, "text", None)
            if isinstance(t, str):
                text = t
            elif isinstance(first, dict):
                text = str(first.get("text", ""))

        usage = getattr(response, "usage", None)
        input_tokens: int | None = None
        if usage is not None:
            input_tokens = (
                getattr(usage, "input_tokens", 0)
                + getattr(usage, "cache_read_input_tokens", 0)
                + getattr(usage, "cache_creation_input_tokens", 0)
            )
        return text, input_tokens

    def _call_via_cli(self, prompt: str) -> tuple[str, int | None]:
        """Shell out to `claude --print --output-format json`.

        Piggybacks on the caller's Claude Code session (no
        ANTHROPIC_API_KEY needed). Uses JSON output to extract both
        the response text and token usage stats.

        Passes prompt via stdin — passing via `-p <prompt>` hits Linux's
        ARG_MAX (~128KB) once the accumulator grows. Runs from /tmp so
        we don't inherit project-specific hooks / settings from cwd.
        """
        result = subprocess.run(
            [
                "claude",
                "--print",
                "--model", self.config.model,
                "--output-format", "json",
            ],
            input=prompt,
            cwd="/tmp",
            capture_output=True,
            text=True,
            timeout=self.config.cli_timeout_s,
        )
        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip()[-400:]
            raise RuntimeError(
                f"claude CLI exited {result.returncode}: {stderr_tail}"
            )
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout, None

        text = envelope.get("result", "")
        usage = envelope.get("usage") or {}
        input_tokens: int | None = None
        if usage:
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
        return text, input_tokens

    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.log is not None:
            self.log(msg)
