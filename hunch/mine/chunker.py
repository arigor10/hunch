"""Chunk a conversation.jsonl into overlapping windows for nose mining.

Windows break only at user-turn boundaries so no utterance is split
mid-sentence.  Overlap ensures nose moments near window edges have
enough surrounding context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Chunk:
    """One window of events."""

    events: list[dict] = field(default_factory=list)
    start_seq: int = 0
    end_seq: int = 0

    @property
    def n_events(self) -> int:
        return len(self.events)


def read_conversation(path: Path) -> list[dict]:
    """Read all events from a conversation.jsonl file."""
    events: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def chunk_conversation(
    events: list[dict],
    window_size: int = 200,
    overlap: int = 50,
) -> list[Chunk]:
    """Split events into overlapping fixed-size windows.

    Windows break at user-turn boundaries: if the nominal end falls
    mid-conversation, the window extends to include all events up to
    (but not including) the next user_text event.

    Each window overlaps with the previous by *overlap* events so that
    nose moments near boundaries have enough context.
    """
    if not events:
        return []

    user_turn_indices = _user_turn_indices(events)
    chunks: list[Chunk] = []
    start = 0

    while start < len(events):
        nominal_end = start + window_size

        if nominal_end >= len(events):
            end = len(events)
        else:
            end = _snap_to_user_turn(nominal_end, user_turn_indices)
            if end <= start:
                end = len(events)

        window_events = events[start:end]
        chunks.append(Chunk(
            events=window_events,
            start_seq=window_events[0].get("tick_seq", 0),
            end_seq=window_events[-1].get("tick_seq", 0),
        ))

        if end >= len(events):
            break

        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


def _user_turn_indices(events: list[dict]) -> list[int]:
    """Return indices of all user_text events."""
    return [i for i, e in enumerate(events) if e.get("type") == "user_text"]


def _snap_to_user_turn(idx: int, user_turn_indices: list[int]) -> int:
    """Find the nearest user_text boundary at or after idx.

    Returns the index of the first user_text event at position >= idx.
    If none exists, returns idx unchanged (caller handles end-of-list).
    """
    for ui in user_turn_indices:
        if ui >= idx:
            return ui
    return idx
