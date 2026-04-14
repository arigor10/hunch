"""Build the per-tick context for the Critic.

Reads the replay buffer and prior hunches, formats them into strings
that the prompt template can splice in. Pure — no API calls, no disk
writes, fully unit-testable.

Owned by the Critic, not by the framework: what ends up in the
prompt is a Critic implementation detail. If we swap Sonnet for
something else, we'll keep the buffer schemas but may change what
gets rendered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunch.journal.feedback import read_labeled_hunch_ids
from hunch.journal.hunches import HunchRecord, read_current_hunches


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextConfig:
    """Knobs for what the Critic sees each tick.

    Defaults line up with critic_v0.md §Context window.
    """
    last_n_chunks: int = 20
    last_m_hunches: int = 10
    artifacts_budget_bytes: int = 20_000  # drop oldest-modified when exceeded


# ---------------------------------------------------------------------------
# Chunk rendering
# ---------------------------------------------------------------------------

def _chunk_id_for_seq(tick_seq: int) -> str:
    """conversation.jsonl rows carry tick_seq but not a chunk id; fall
    back to a deterministic c-NNNN derived from tick_seq so the prompt
    has stable, citable IDs.
    """
    return f"c-{tick_seq:04d}"


def _render_conversation_event(entry: dict[str, Any]) -> str:
    """Render one event from conversation.jsonl as compact dialogue text.

    We don't try to reconstruct full chunks here — this is just a flat
    event stream the Critic can scan. Keeping it flat sidesteps the
    chunk-boundary-reconstruction work the miner does offline.
    """
    etype = entry.get("type", "?")
    seq = entry.get("tick_seq", "?")
    cid = _chunk_id_for_seq(seq) if isinstance(seq, int) else "c-????"
    ts = entry.get("timestamp", "")

    if etype == "text":
        role = entry.get("role", "?")
        text = entry.get("content") or entry.get("text") or ""
        return f"[{cid}] ({role}) {text}"
    if etype == "artifact_write":
        path = entry.get("path", "?")
        return f"[{cid}] (artifact-write) {path}"
    if etype == "artifact_edit":
        path = entry.get("path", "?")
        skipped = entry.get("skipped_reason")
        if skipped:
            return f"[{cid}] (artifact-edit-skipped: {skipped}) {path}"
        return f"[{cid}] (artifact-edit) {path}"
    if etype == "figure":
        path = entry.get("path", "?")
        return f"[{cid}] (figure) {path}"
    if etype == "tool_error":
        return f"[{cid}] (tool-error) {entry.get('message', '')}"
    # Fall-through for forward-compat
    return f"[{cid}] ({etype}) {ts}"


def read_recent_conversation(
    conversation_path: Path,
    last_n: int,
) -> list[str]:
    """Return the last `last_n` events from conversation.jsonl, rendered
    as one-line strings. Returns `[]` if the file is absent or empty.

    Reads the full file every tick. That's fine for v0 (sessions are
    bounded and the file is small). A tail-reader is an obvious later
    optimization if it matters.
    """
    if not conversation_path.exists():
        return []
    rendered: list[str] = []
    with open(conversation_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            rendered.append(_render_conversation_event(entry))
    if last_n <= 0:
        return []
    return rendered[-last_n:]


# ---------------------------------------------------------------------------
# Artifact rendering
# ---------------------------------------------------------------------------

def read_current_artifacts(
    artifacts_dir: Path,
    artifacts_log_path: Path,
    budget_bytes: int,
) -> list[tuple[str, str]]:
    """Return `[(relative_path, content), ...]` for currently-live .md
    artifacts, sized to fit within `budget_bytes`.

    "Currently-live" = the most recent snapshot per path, according to
    the artifacts.jsonl log. We oldest-first-drop to stay under budget,
    so when budget bites, the most recently edited artifacts are the
    ones the Critic sees. Matches critic_v0.md §Context window.
    """
    if not artifacts_log_path.exists():
        return []

    latest_by_path: dict[str, tuple[str, str]] = {}  # path -> (snapshot_name, ts)
    with open(artifacts_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = entry.get("event")
            if evt not in ("write", "edit"):
                continue
            path = entry.get("path")
            snap = entry.get("snapshot")
            ts = entry.get("ts", "")
            if not path or not snap:
                continue
            if not path.endswith(".md"):
                continue
            latest_by_path[path] = (snap, ts)

    # Oldest first so we drop oldest when we exceed budget.
    ordered = sorted(
        latest_by_path.items(),
        key=lambda kv: kv[1][1],  # ts
    )

    results: list[tuple[str, str]] = []
    total = 0
    # Walk newest-first so we keep the newest N that fit, drop older.
    for path, (snap, _ts) in reversed(ordered):
        snap_path = artifacts_dir / snap
        if not snap_path.exists():
            continue
        try:
            content = snap_path.read_text()
        except OSError:
            continue
        size = len(content.encode("utf-8", errors="replace"))
        if total + size > budget_bytes and results:
            break
        results.insert(0, (path, content))  # keep original (oldest-first) order
        total += size
    return results


def render_artifacts_block(artifacts: list[tuple[str, str]]) -> str:
    if not artifacts:
        return "(no .md artifacts in the replay buffer yet)"
    parts = []
    for path, content in artifacts:
        parts.append(f"### {path}\n\n{content}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Prior-hunches rendering
# ---------------------------------------------------------------------------

def render_prior_hunches_block(
    hunches_path: Path,
    feedback_path: Path,
    last_m: int,
) -> str:
    """Render the most recent `last_m` hunches with their statuses and
    any explicit labels. Returns a plain-text block (not JSON) so the
    model reads it as commentary, not data to echo back.
    """
    records = read_current_hunches(hunches_path)
    if not records:
        return "(no prior hunches)"
    labels = read_labeled_hunch_ids(feedback_path)
    recent = records[-last_m:] if last_m > 0 else records
    lines = []
    for r in recent:
        label = labels.get(r.hunch_id, "")
        label_str = f" [labeled: {label}]" if label else ""
        lines.append(
            f"- {r.hunch_id} ({r.status}){label_str}: {r.smell}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TickContext:
    """Everything the Critic needs for one tick, as formatted strings.

    The dataclass lets tests assert against specific rendered blocks
    without stubbing the whole prompt-rendering pipeline.
    """
    prior_hunches_block: str
    recent_chunks_block: str
    artifacts_block: str


def build_tick_context(
    replay_dir: Path,
    config: ContextConfig | None = None,
) -> TickContext:
    """Read the replay buffer and assemble the tick context."""
    cfg = config or ContextConfig()
    replay_dir = Path(replay_dir)

    recent = read_recent_conversation(
        replay_dir / "conversation.jsonl",
        last_n=cfg.last_n_chunks,
    )
    artifacts = read_current_artifacts(
        replay_dir / "artifacts",
        replay_dir / "artifacts.jsonl",
        budget_bytes=cfg.artifacts_budget_bytes,
    )
    prior = render_prior_hunches_block(
        replay_dir / "hunches.jsonl",
        replay_dir / "feedback.jsonl",
        last_m=cfg.last_m_hunches,
    )

    return TickContext(
        prior_hunches_block=prior,
        recent_chunks_block=(
            "\n".join(recent) if recent else "(no conversation events yet)"
        ),
        artifacts_block=render_artifacts_block(artifacts),
    )
