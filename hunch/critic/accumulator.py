"""Accumulating Critic prompt stream (v1).

See `docs/critic_v1.md` for the full design note. Quick summary:

The Critic's prompt is laid out as four regions that always appear
in this order in the rendered output:

    ┌── 1. Static preamble ────────────────────┐
    │ system prompt + Critic instructions       │
    ├── 2. Surviving open hunches ─────────────┤
    │ rebuilt only at purge; empty initially    │
    ├── 3. Living artifacts snapshot ──────────┤
    │ rebuilt only at purge; empty initially    │
    ├── 4. Timeline — pure append ─────────────┤
    │ chunk events, inline hunches, labels,     │
    │ artifact writes/edits                     │
    └───────────────────────────────────────────┘

Invariant: during normal operation nothing gets rewritten, only
appended. Prefix identity holds → Sonnet's prompt cache hits on
everything up to the last tick.

Regions 2 and 3 are rebuilt exactly when we purge — i.e. when
cumulative token count crosses the high watermark. Purge trims the
front of the timeline, moves any still-open hunches into region 2,
and snapshots the current content of every live .md into region 3.
That is one cache miss; everything after it accumulates cheaply.

Token bookkeeping is based on `usage.input_tokens` from the model
response: we record the observed prefix size after each call, and
only estimate the delta contributed by events appended since then.

Purely in-memory, purely testable. The sim driver in
`scripts/critic_sim.py` feeds events in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Union

from hunch.critic.protocol import Hunch, TriggeringRefs


INPUTS_MARKER = "<!-- INPUTS_GO_HERE -->"


def load_prompt_template(prompt_path: str | Path) -> tuple[str, str]:
    """Split a v1 prompt file into (preamble, suffix) on the inputs marker.

    The accumulator inserts its rendered regions between the two halves
    at render time. If the marker is missing, the whole file becomes the
    preamble and the suffix is empty — useful for tests.
    """
    raw = Path(prompt_path).read_text()
    if INPUTS_MARKER not in raw:
        return raw.rstrip() + "\n", ""
    head, tail = raw.split(INPUTS_MARKER, 1)
    return head.rstrip() + "\n", tail.lstrip()


# ---------------------------------------------------------------------------
# Timeline event types
# ---------------------------------------------------------------------------

def chunk_id_for_seq(seq: int) -> str:
    return f"c-{seq:04d}"


@dataclass(frozen=True)
class ChunkTextEvent:
    """One textual utterance from the dialogue stream."""
    tick_seq: int
    role: Literal["user", "assistant"]
    text: str

    @property
    def chunk_id(self) -> str:
        return chunk_id_for_seq(self.tick_seq)


@dataclass(frozen=True)
class ArtifactWriteEvent:
    """A full-content write of a .md artifact at the time it occurred."""
    tick_seq: int
    path: str
    content: str

    @property
    def chunk_id(self) -> str:
        return chunk_id_for_seq(self.tick_seq)


@dataclass(frozen=True)
class ArtifactEditEvent:
    """An inline old/new edit to a previously-written artifact."""
    tick_seq: int
    path: str
    old_string: str
    new_string: str

    @property
    def chunk_id(self) -> str:
        return chunk_id_for_seq(self.tick_seq)


@dataclass(frozen=True)
class InlineHunchEvent:
    """A hunch emission, anchored at the tick it fired."""
    tick_seq: int
    hunch_id: str
    smell: str
    description: str
    triggering_refs: TriggeringRefs = field(default_factory=TriggeringRefs)

    @property
    def chunk_id(self) -> str:
        return chunk_id_for_seq(self.tick_seq)


@dataclass(frozen=True)
class LabelEvent:
    """A Scientist feedback label for an earlier hunch."""
    tick_seq: int
    hunch_id: str
    label: Literal["good", "bad", "skip"]

    @property
    def chunk_id(self) -> str:
        return chunk_id_for_seq(self.tick_seq)


TimelineEvent = Union[
    ChunkTextEvent,
    ArtifactWriteEvent,
    ArtifactEditEvent,
    InlineHunchEvent,
    LabelEvent,
]


# ---------------------------------------------------------------------------
# Event → string rendering
# ---------------------------------------------------------------------------

def _render_event(event: TimelineEvent) -> str:
    if isinstance(event, ChunkTextEvent):
        return f"[{event.chunk_id}] ({event.role}) {event.text}"
    if isinstance(event, ArtifactWriteEvent):
        return (
            f"[{event.chunk_id}] (artifact-write) {event.path}\n"
            f"```\n{event.content}\n```"
        )
    if isinstance(event, ArtifactEditEvent):
        return (
            f"[{event.chunk_id}] (artifact-edit) {event.path}\n"
            f"- old:\n```\n{event.old_string}\n```\n"
            f"- new:\n```\n{event.new_string}\n```"
        )
    if isinstance(event, InlineHunchEvent):
        refs = event.triggering_refs
        refs_line = ""
        if refs.chunks or refs.artifacts:
            parts = []
            if refs.chunks:
                parts.append(f"chunks: {', '.join(refs.chunks)}")
            if refs.artifacts:
                parts.append(f"artifacts: {', '.join(refs.artifacts)}")
            refs_line = f"\n  triggering_refs — {'; '.join(parts)}"
        return (
            f"[{event.chunk_id}] (critic-hunch {event.hunch_id}) "
            f"{event.smell}\n  {event.description}{refs_line}"
        )
    if isinstance(event, LabelEvent):
        return (
            f"[{event.chunk_id}] (scientist-label {event.hunch_id}) "
            f"{event.label}"
        )
    # mypy exhaustiveness guard; should be unreachable
    raise TypeError(f"unknown timeline event type: {type(event).__name__}")


def _render_surviving_hunches_block(hunches: list[InlineHunchEvent]) -> str:
    if not hunches:
        return "(no open hunches carried over)"
    lines = []
    for h in hunches:
        refs = h.triggering_refs
        refs_parts = []
        if refs.chunks:
            refs_parts.append(f"chunks: {', '.join(refs.chunks)}")
        if refs.artifacts:
            refs_parts.append(f"artifacts: {', '.join(refs.artifacts)}")
        refs_line = f" [{'; '.join(refs_parts)}]" if refs_parts else ""
        lines.append(
            f"- {h.hunch_id} (emitted at {h.chunk_id}){refs_line}: {h.smell}\n"
            f"    {h.description}"
        )
    return "\n".join(lines)


def _render_living_artifacts_block(artifacts: dict[str, str]) -> str:
    if not artifacts:
        return "(no .md artifacts at the purge boundary)"
    parts = []
    for path in sorted(artifacts):
        parts.append(f"### {path}\n\n{artifacts[path]}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Token bookkeeping
# ---------------------------------------------------------------------------

# Rough chars→tokens ratio. 4 chars/token is the Anthropic rule of thumb
# for English; we deliberately err on the high side (lower ratio) when
# estimating the delta so we purge slightly early rather than slightly
# late. Configurable on the stream.
DEFAULT_CHARS_PER_TOKEN = 3.5


def _estimate_tokens(text: str, chars_per_token: float) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token) + 1)


# ---------------------------------------------------------------------------
# The prompt stream
# ---------------------------------------------------------------------------

@dataclass
class CriticPromptStream:
    """Append-only prompt stream with purge-based compaction.

    Usage:
      1. Construct with the static `preamble` (system prompt + Critic
         instructions — the part that never changes within a session).
      2. Append events as they happen via `append()`.
      3. Before each model call, check `should_purge()`. If true, call
         `purge()` then render.
      4. Call `render()` to get the prompt string.
      5. After the model call, record the observed `usage.input_tokens`
         via `update_observed_tokens()`. This anchors future projections.
    """
    preamble: str
    suffix: str = ""
    low_watermark: int = 150_000
    high_watermark: int = 200_000
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN

    # Budget in tokens for the rebuilt living-artifacts block. When the
    # current .md corpus exceeds this at purge time, we evict
    # least-recently-touched files first until we fit. Default ≈ half
    # of low_watermark so the timeline has room to grow after purge.
    artifact_budget_tokens: int | None = None

    surviving_hunches: list[InlineHunchEvent] = field(default_factory=list)
    living_artifacts: dict[str, str] = field(default_factory=dict)
    timeline: list[TimelineEvent] = field(default_factory=list)

    # Running content of every .md ever written, used as the snapshot
    # source at purge time. Mutated on every write/edit event.
    _current_artifacts: dict[str, str] = field(default_factory=dict)
    # Last tick_seq at which each path was written or edited. Used as
    # the eviction ordering key when the artifact budget bites.
    _artifact_touched_at: dict[str, int] = field(default_factory=dict)

    # Ground-truth token count of the last-rendered prompt, as reported
    # by the model's usage.input_tokens. None until we've seen one.
    _observed_prefix_tokens: int | None = field(default=None, repr=False)
    # len(timeline) at the moment the observation was taken. Events
    # after this index are the ones we have to estimate.
    _observed_timeline_len: int = field(default=0, repr=False)
    # Per-event token contribution, parallel to `timeline`. Populated
    # on each `update_observed_tokens` by distributing the observed
    # timeline-portion tokens across events by character weight. Zero
    # until the first observation. Lets `purge()` walk backward with
    # exact per-event contributions instead of the chars/cpt estimate.
    _event_tokens: list[float] = field(default_factory=list, repr=False)
    # Empirical chars-per-token ratio derived from observations. EMA
    # over `rendered_chars / input_tokens`. None until the first obs.
    # Used for artifact budgeting and post-purge fixed-region estimates
    # so the default `chars_per_token=3.5` doesn't systematically
    # under-count tokens for markdown-heavy prompts (~2.9 in practice).
    _empirical_chars_per_token: float | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Append API
    # ------------------------------------------------------------------

    def append_chunk_text(
        self,
        tick_seq: int,
        role: Literal["user", "assistant"],
        text: str,
    ) -> ChunkTextEvent:
        event = ChunkTextEvent(tick_seq=tick_seq, role=role, text=text)
        self.timeline.append(event)
        self._event_tokens.append(0.0)
        return event

    def append_artifact_write(
        self,
        tick_seq: int,
        path: str,
        content: str,
    ) -> ArtifactWriteEvent:
        event = ArtifactWriteEvent(tick_seq=tick_seq, path=path, content=content)
        self.timeline.append(event)
        self._event_tokens.append(0.0)
        self._current_artifacts[path] = content
        self._artifact_touched_at[path] = tick_seq
        return event

    def append_artifact_edit(
        self,
        tick_seq: int,
        path: str,
        old_string: str,
        new_string: str,
    ) -> ArtifactEditEvent:
        event = ArtifactEditEvent(
            tick_seq=tick_seq,
            path=path,
            old_string=old_string,
            new_string=new_string,
        )
        self.timeline.append(event)
        self._event_tokens.append(0.0)
        base = self._current_artifacts.get(path)
        if base is None:
            # Edit-before-write: we don't have a base to apply to, but
            # region 3 may carry prior content if this edit arrives
            # post-purge. Consult living_artifacts as a fallback source.
            base = self.living_artifacts.get(path)
        if base is not None and old_string in base:
            self._current_artifacts[path] = base.replace(old_string, new_string, 1)
            self._artifact_touched_at[path] = tick_seq
        return event

    def append_hunch(
        self,
        tick_seq: int,
        hunch_id: str,
        hunch: Hunch,
    ) -> InlineHunchEvent:
        event = InlineHunchEvent(
            tick_seq=tick_seq,
            hunch_id=hunch_id,
            smell=hunch.smell,
            description=hunch.description,
            triggering_refs=hunch.triggering_refs,
        )
        self.timeline.append(event)
        self._event_tokens.append(0.0)
        return event

    def append_label(
        self,
        tick_seq: int,
        hunch_id: str,
        label: Literal["good", "bad", "skip"],
    ) -> LabelEvent:
        event = LabelEvent(tick_seq=tick_seq, hunch_id=hunch_id, label=label)
        self.timeline.append(event)
        self._event_tokens.append(0.0)
        return event

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> str:
        surviving_block = _render_surviving_hunches_block(self.surviving_hunches)
        artifacts_block = _render_living_artifacts_block(self.living_artifacts)
        timeline_block = (
            "\n\n".join(_render_event(e) for e in self.timeline)
            if self.timeline
            else "(timeline is empty)"
        )
        rendered = (
            self.preamble
            + "\n\n## Open hunches carried over from earlier\n\n"
            + surviving_block
            + "\n\n## Current state of .md artifacts\n\n"
            + artifacts_block
            + "\n\n## Timeline\n\n"
            + timeline_block
        )
        if self.suffix:
            rendered += "\n\n" + self.suffix
        return rendered

    # ------------------------------------------------------------------
    # Token bookkeeping
    # ------------------------------------------------------------------

    def _effective_chars_per_token(self) -> float:
        """Best available chars/token ratio: empirical if we've seen one,
        otherwise the configured default. Empirical settles near ~2.9
        for markdown-heavy Critic prompts; the 3.5 default over-counts
        chars per token, which silently under-estimates real token usage.
        """
        return self._empirical_chars_per_token or self.chars_per_token

    def _estimate_fixed_region_tokens(self) -> int:
        """Estimate tokens in the non-timeline regions of the current
        rendered prompt (preamble + surviving hunches + living artifacts
        + section headers + optional suffix)."""
        fixed_chars = (
            len(self.preamble)
            + len(_render_surviving_hunches_block(self.surviving_hunches))
            + len(_render_living_artifacts_block(self.living_artifacts))
            + 200  # section headers / separators
        )
        if self.suffix:
            fixed_chars += len(self.suffix) + 2
        return _estimate_tokens(" " * fixed_chars, self._effective_chars_per_token())

    def update_observed_tokens(self, input_tokens: int) -> None:
        """Record the model-reported prefix size after a successful call.

        Anchors future projections AND re-attributes per-event token
        contributions from the observation so `purge()` can walk with
        exact numbers. Also updates the empirical chars/token ratio
        (EMA), which feeds into artifact budgeting and fixed-region
        estimates for the next tick.
        """
        input_tokens = int(input_tokens)

        # Update empirical chars/token ratio (EMA, 0.3 weight on new).
        rendered_chars = len(self.render())
        if input_tokens > 0:
            empirical = rendered_chars / input_tokens
            if self._empirical_chars_per_token is None:
                self._empirical_chars_per_token = empirical
            else:
                self._empirical_chars_per_token = (
                    0.3 * empirical + 0.7 * self._empirical_chars_per_token
                )

        # Re-attribute the timeline-portion tokens across events by
        # character weight. After this, `sum(_event_tokens)` tracks the
        # observed timeline token total — so when purge() walks backward
        # using _event_tokens[i], it uses observed mass, not an estimate.
        fixed_tokens = self._estimate_fixed_region_tokens()
        timeline_tokens_total = max(0.0, float(input_tokens - fixed_tokens))
        if self.timeline:
            char_weights = [len(_render_event(e)) + 2 for e in self.timeline]
            total_chars = sum(char_weights) or 1
            self._event_tokens = [
                timeline_tokens_total * (w / total_chars) for w in char_weights
            ]
        else:
            self._event_tokens = []

        self._observed_prefix_tokens = input_tokens
        self._observed_timeline_len = len(self.timeline)

    def projected_tokens(self) -> int:
        """Best estimate of the current prompt's token count.

        If we have an observation, project from it by estimating the
        size of events appended since. Otherwise estimate the whole
        rendered prompt directly. Uses the empirical chars/token ratio
        when one exists so the default 3.5 doesn't systematically
        under-count.
        """
        cpt = self._effective_chars_per_token()
        if self._observed_prefix_tokens is None:
            return _estimate_tokens(self.render(), cpt)

        delta_chars = 0
        for event in self.timeline[self._observed_timeline_len:]:
            delta_chars += len(_render_event(event)) + 2  # +2 for "\n\n" separator
        delta_tokens = _estimate_tokens(" " * delta_chars, cpt) if delta_chars else 0
        return self._observed_prefix_tokens + delta_tokens

    def should_purge(self) -> bool:
        return self.projected_tokens() >= self.high_watermark

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------

    def purge(self) -> int:
        """Trim the front of the timeline until projected tokens ≤ low
        watermark. Rebuild surviving_hunches and living_artifacts.

        Returns the number of timeline events dropped.

        Uses per-event observed token contributions (populated by prior
        `update_observed_tokens` calls) when available — that makes the
        backward walk exact for the observed portion of the timeline.
        Events that were appended since the last observation fall back
        to the chars/cpt estimate.
        """
        open_hunches = self._collect_open_hunches()
        # Would-be post-purge artifact snapshot (budgeted). Computed
        # once — we need it both for the fixed-region sizing below and
        # as the new `living_artifacts` value afterwards.
        snap = self._budgeted_artifacts_snapshot()

        cpt = self._effective_chars_per_token()
        fixed_chars = (
            len(self.preamble)
            + len(_render_surviving_hunches_block(open_hunches))
            + len(_render_living_artifacts_block(snap))
            + 200  # section headers / separators, flat overhead
        )
        if self.suffix:
            fixed_chars += len(self.suffix) + 2
        fixed_tokens = _estimate_tokens(" " * fixed_chars, cpt)

        target_timeline_tokens = max(0, self.low_watermark - fixed_tokens)

        # Walk timeline backward, summing until we hit the budget.
        running = 0.0
        cutoff = 0  # index — keep timeline[cutoff:]
        for i in range(len(self.timeline) - 1, -1, -1):
            observed = (
                self._event_tokens[i] if i < len(self._event_tokens) else 0.0
            )
            if observed > 0:
                ev_tokens: float = observed
            else:
                ev_chars = len(_render_event(self.timeline[i])) + 2
                ev_tokens = float(_estimate_tokens(" " * ev_chars, cpt))
            if running + ev_tokens > target_timeline_tokens:
                cutoff = i + 1
                break
            running += ev_tokens
        else:
            # Loop completed without breaking: whole timeline fits.
            cutoff = 0

        dropped = self.timeline[:cutoff]
        kept = self.timeline[cutoff:]
        kept_event_tokens = self._event_tokens[cutoff:]

        # Hunches that survive: open ones whose inline event is NOT in
        # kept (otherwise they're already inline and we don't dedupe).
        kept_inline_ids = {
            e.hunch_id for e in kept if isinstance(e, InlineHunchEvent)
        }
        new_surviving = [
            h for h in open_hunches if h.hunch_id not in kept_inline_ids
        ]

        self.surviving_hunches = new_surviving
        self.living_artifacts = snap
        self.timeline = kept
        self._event_tokens = kept_event_tokens

        # Synthesize a post-purge observation so the next `should_purge`
        # check doesn't rebuild its estimate from scratch: our best
        # guess is (fresh fixed-region estimate) + (observed tokens of
        # kept events). The next real `update_observed_tokens` call will
        # correct the fixed-region part.
        post_fixed_tokens = self._estimate_fixed_region_tokens()
        self._observed_prefix_tokens = (
            post_fixed_tokens + int(round(sum(kept_event_tokens)))
        )
        self._observed_timeline_len = len(self.timeline)

        return len(dropped)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _budgeted_artifacts_snapshot(self) -> dict[str, str]:
        """Build the living-artifacts snapshot, evicting LRU by tick_seq
        of last touch when the total exceeds `artifact_budget_tokens`.

        Default budget is half of `low_watermark` tokens — keeps the
        timeline plenty of room to grow post-purge. The invariant we
        uphold: the rendered artifacts block fits in the budget, even if
        that means dropping files the Critic has never looked at.
        """
        if not self._current_artifacts:
            return {}

        budget_tokens = (
            self.artifact_budget_tokens
            if self.artifact_budget_tokens is not None
            else self.low_watermark // 2
        )
        budget_chars = int(budget_tokens * self._effective_chars_per_token())

        # Rank paths newest-touched-first so we keep the most recently
        # edited files when the budget bites.
        ranked = sorted(
            self._current_artifacts.keys(),
            key=lambda p: self._artifact_touched_at.get(p, 0),
            reverse=True,
        )

        snapshot: dict[str, str] = {}
        total = 0
        for path in ranked:
            content = self._current_artifacts[path]
            # Approximate per-entry overhead: "### path\n\n" + "\n\n---\n\n"
            size = len(content) + len(path) + 15
            if total + size > budget_chars and snapshot:
                break
            snapshot[path] = content
            total += size
        return snapshot

    def _collect_open_hunches(self) -> list[InlineHunchEvent]:
        """Return currently-emitted hunches that have no matching label.

        Walks in emission order across surviving_hunches then timeline.
        """
        labeled_ids: set[str] = set()
        for e in self.timeline:
            if isinstance(e, LabelEvent):
                labeled_ids.add(e.hunch_id)

        emitted: list[InlineHunchEvent] = []
        seen_ids: set[str] = set()
        for h in self.surviving_hunches:
            if h.hunch_id not in seen_ids:
                emitted.append(h)
                seen_ids.add(h.hunch_id)
        for e in self.timeline:
            if isinstance(e, InlineHunchEvent) and e.hunch_id not in seen_ids:
                emitted.append(e)
                seen_ids.add(e.hunch_id)

        return [h for h in emitted if h.hunch_id not in labeled_ids]
