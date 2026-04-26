"""Model-agnostic Critic Engine (v0.1).

Holds a CriticPromptStream across ticks, feeds conversation events,
syncs hunches/labels, renders prompts, and delegates the actual model
call to an injected Backend.

See sonnet.py for the backward-compatible SonnetCritic wrapper that
constructs a Backend and delegates to this engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import re

from hunch.backend.protocol import Backend, ModelResponse
from hunch.critic.accumulator import (
    CriticPromptStream,
    load_prompt_template,
)
from hunch.critic.protocol import Hunch, TriggeringRefs


# ---------------------------------------------------------------------------
# Response parsing (shared by all backends)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


def parse_response(text: str) -> list[Hunch]:
    """Parse the model response into a list of Hunches.

    The prompt asks for a raw JSON array. We tolerate accidental code
    fences and prose-preamble on a best-effort basis — if parsing fails
    at any level we return `[]` and let the caller log.
    """
    stripped = _strip_fences(text)
    bracket = stripped.find("[")
    if bracket == -1:
        return []
    candidate = stripped[bracket:]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    hunches: list[Hunch] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        smell = item.get("smell")
        description = item.get("description")
        if not isinstance(smell, str) or not isinstance(description, str):
            continue
        if not smell.strip() or not description.strip():
            continue
        refs_raw = item.get("triggering_refs") or {}
        if not isinstance(refs_raw, dict):
            refs_raw = {}
        chunks = refs_raw.get("chunks") or []
        artifacts = refs_raw.get("artifacts") or []
        if not isinstance(chunks, list):
            chunks = []
        if not isinstance(artifacts, list):
            artifacts = []
        hunches.append(
            Hunch(
                smell=smell.strip(),
                description=description.strip(),
                triggering_refs=TriggeringRefs(
                    chunks=[str(c) for c in chunks],
                    artifacts=[str(a) for a in artifacts],
                ),
            )
        )
    return hunches


@dataclass(frozen=True)
class CriticEngineConfig:
    prompt_path: Path | None = None
    low_watermark: int = 140_000
    high_watermark: int = 180_000
    max_consecutive_failures: int = 3
    dry_run: bool = False


@dataclass
class CriticEngine:
    """Model-agnostic critic engine.

    Construct with an injected Backend. The framework calls init(),
    tick(), shutdown() — same protocol as SonnetCritic.
    """
    backend: Backend
    config: CriticEngineConfig = field(default_factory=CriticEngineConfig)
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
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _total_input_tokens: int = field(default=0, init=False, repr=False)
    _total_output_tokens: int = field(default=0, init=False, repr=False)
    _total_cached_tokens: int = field(default=0, init=False, repr=False)
    _total_calls: int = field(default=0, init=False, repr=False)
    _total_failures: int = field(default=0, init=False, repr=False)
    _prev_prompt_len: int = field(default=0, init=False, repr=False)

    # ------------------------------------------------------------------
    # Critic protocol
    # ------------------------------------------------------------------

    def init(self, config: dict[str, Any]) -> None:
        if self._initialized:
            raise RuntimeError("CriticEngine.init called twice")
        replay_dir = config.get("replay_dir")
        if not replay_dir:
            raise RuntimeError("CriticEngine.init: 'replay_dir' missing from config")
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
            raise RuntimeError("CriticEngine.tick called before init")

        appended = self._feed_conversation(bookmark_now)
        hunches_seen = self._sync_hunches(bookmark_now)
        labels_seen = self._sync_labels(bookmark_now)

        purged = 0
        if self._stream.should_purge():
            purged = self._stream.purge()
            self._prev_prompt_len = 0

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
            cache_break = self._prev_prompt_len or None
            response: ModelResponse = self.backend.call(prompt, cache_break=cache_break)
        except Exception as e:
            self._consecutive_failures += 1
            self._total_failures += 1
            self._log(
                f"[critic] model call failed ({self._consecutive_failures}/"
                f"{self.config.max_consecutive_failures}): {e}"
            )
            if self._consecutive_failures >= self.config.max_consecutive_failures:
                raise RuntimeError(
                    f"Critic aborting: {self._consecutive_failures} consecutive "
                    f"model failures. Last error: {e}"
                ) from e
            return []

        self._consecutive_failures = 0
        self._total_calls += 1
        self._prev_prompt_len = len(prompt)
        text = response.text
        input_tokens = response.input_tokens
        if input_tokens:
            self._total_input_tokens += input_tokens
        if response.output_tokens:
            self._total_output_tokens += response.output_tokens
        if response.cached_tokens:
            self._total_cached_tokens += response.cached_tokens

        if input_tokens is not None and input_tokens > 0:
            pre_proj = projected
            self._stream.update_observed_tokens(input_tokens)
            self._log(
                f"[critic] {tick_id} prompt_chars={len(prompt):,} "
                f"input_tokens={input_tokens:,} "
                f"est_tokens={pre_proj:,} "
                f"err={input_tokens - pre_proj:+,}"
            )
        elif input_tokens == 0:
            self._log(
                f"[critic] {tick_id} prompt_chars={len(prompt):,} "
                f"input_tokens=0 (skipped estimation update)"
            )

        hunches = parse_response(text)
        if not hunches:
            self._log("[critic] (no hunches this tick)")
        return hunches

    def shutdown(self) -> None:
        self._initialized = False
        if hasattr(self.backend, "shutdown"):
            self.backend.shutdown()

    def stats(self) -> dict[str, Any]:
        """Return accumulated run stats."""
        cache_pct = 0.0
        if self._total_input_tokens > 0:
            cache_pct = 100.0 * self._total_cached_tokens / self._total_input_tokens
        s: dict[str, Any] = {
            "calls": self._total_calls,
            "failures": self._total_failures,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "cached_tokens": self._total_cached_tokens,
            "cache_hit_pct": round(cache_pct, 1),
        }
        if hasattr(self.backend, "total_cost"):
            s["total_cost_usd"] = self.backend.total_cost()
        return s

    # ------------------------------------------------------------------
    # Stream feeding
    # ------------------------------------------------------------------

    def _feed_conversation(self, up_to_seq: int) -> int:
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
                    self._conv_cursor = line_start
                    break
                if seq <= self._last_seq_fed:
                    continue
                if self._append_event(entry):
                    count += 1
                self._last_seq_fed = seq
        return count

    def _append_event(self, entry: dict[str, Any]) -> bool:
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
    # Hunches / labels sync
    # ------------------------------------------------------------------

    def _sync_hunches(self, up_to_seq: int) -> int:
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

    def _log(self, msg: str) -> None:
        if self.log is not None:
            self.log(msg)
