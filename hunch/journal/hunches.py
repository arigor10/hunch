"""Writer + status-folding reader for `.hunch/replay/hunches.jsonl`.

The file is event-sourced: each line is either an `emit` event (one
per hunch, written by the framework when a Critic tick returns hunches)
or a `status_change` event (zero or more per hunch, written by the
side panel, the UserPromptSubmit hook, the framework itself, or — in
the future — an agentic Critic that wants to mark its own hunches).

Append-only. No in-place mutation anywhere. Current state is derived
by folding events in timestamp order.

Hunch ids are `h-NNNN` strings; the writer allocates them monotonically
per-process from the highest id it sees on disk at startup. That's
enough for v0 where only one framework process writes at a time.
Future multi-writer setups will need a stricter allocation scheme
(see "ID allocation" in docstring below).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hunch.critic import Hunch, hunch_emit_record
from hunch.journal.append import append_json_line


_HUNCH_ID_RE = re.compile(r"^h-(\d+)$")


@dataclass
class HunchRecord:
    """Current-state view of a hunch, derived from its event history.

    Built by `read_current_hunches()` by folding emit + status_change
    events. `status` starts as `"pending"` on emit and moves through
    whatever strings status_change events assign. The string set is
    open — the surface and hooks grow it as new status values matter.

    `history` preserves the ordered list of status-change events for
    debugging, audit, and anyone who wants to see the lifecycle
    (e.g. "was this hunch ever good_pending_inject before being
    suppressed?").
    """
    hunch_id: str
    emitted_ts: str
    emitted_by_tick: int
    smell: str
    description: str
    triggering_refs: dict[str, list[str]]
    status: str
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HunchesWriter:
    """Append-only writer for `hunches.jsonl`.

    Owns:
      - `hunches_path`: the JSONL file.
      - `_next_id_num`: the next numeric id to allocate (initialized
        by scanning existing ids on disk).

    Does NOT own:
      - Timestamp generation (caller passes `ts`). Keeps this module
        easy to test and makes the source-of-truth for "now" explicit.
      - Fold-on-read state (that lives in `read_current_hunches`).

    ID allocation: scans existing emit events on disk at startup and
    picks `max_id + 1`. For v0's single-writer framework process this
    is sufficient. Concurrent writers would race on `_next_id_num`;
    revisit if/when an agentic Critic writes directly.
    """
    hunches_path: Path
    _next_id_num: int = 1

    def __post_init__(self) -> None:
        self.hunches_path = Path(self.hunches_path)
        self.hunches_path.parent.mkdir(parents=True, exist_ok=True)
        self._next_id_num = self._scan_max_id() + 1

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def allocate_id(self) -> str:
        """Reserve the next `h-NNNN` id. Called by the framework before
        writing an emit event."""
        hid = f"h-{self._next_id_num:04d}"
        self._next_id_num += 1
        return hid

    def write_emit(
        self,
        hunch: Hunch,
        hunch_id: str,
        ts: str,
        emitted_by_tick: int,
    ) -> None:
        """Append an emit event for a freshly-minted hunch.

        The hunch's initial status is implicit: any reader folding
        events treats an emit as `status="pending"` until a later
        status_change event says otherwise.
        """
        record = hunch_emit_record(hunch, hunch_id, ts, emitted_by_tick)
        self._append(record)

    def write_status_change(
        self,
        hunch_id: str,
        new_status: str,
        ts: str,
        by: str,
    ) -> None:
        """Append a status_change event for an existing hunch.

        `by` identifies who changed the status — e.g.
        `"scientist_key:alt_g"`, `"hook:user_prompt_submit"`,
        `"critic:self_suppress"`. Free-form string in v0; the surface
        and hook will converge on a small vocabulary.
        """
        self._append(
            {
                "type": "status_change",
                "hunch_id": hunch_id,
                "ts": ts,
                "new_status": new_status,
                "by": by,
            }
        )

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _scan_max_id(self) -> int:
        """Find the largest existing `h-NNNN` id on disk (or 0 if empty)."""
        if not self.hunches_path.exists():
            return 0
        max_n = 0
        with open(self.hunches_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                hid = d.get("hunch_id", "")
                m = _HUNCH_ID_RE.match(hid)
                if m:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
        return max_n

    def _append(self, entry: dict[str, Any]) -> None:
        append_json_line(self.hunches_path, entry)


# ---------------------------------------------------------------------------
# Fold-on-read
# ---------------------------------------------------------------------------

def read_current_hunches(hunches_path: str | Path) -> list[HunchRecord]:
    """Read `hunches.jsonl` and return current-state records.

    Folds events in *file order* (which is append order, which — because
    the writer is single-threaded — is also timestamp order in v0).
    If the file doesn't exist yet, returns `[]`.

    Unknown event types are skipped silently (forward-compatibility:
    future versions may add event types that older readers shouldn't
    crash on). Status-change events for a hunch_id that was never
    emitted are also skipped — they're a writer bug if they happen, but
    crashing the reader helps nobody.
    """
    hunches_path = Path(hunches_path)
    if not hunches_path.exists():
        return []

    records: dict[str, HunchRecord] = {}
    order: list[str] = []  # preserves emit order for the return value

    with open(hunches_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = d.get("type")
            hid = d.get("hunch_id")
            if not hid:
                continue

            if etype == "emit":
                if hid in records:
                    # Duplicate emit for the same id — keep the first,
                    # ignore the second. Dedup contract from framework_v0 §3.
                    continue
                records[hid] = HunchRecord(
                    hunch_id=hid,
                    emitted_ts=d.get("ts", ""),
                    emitted_by_tick=d.get("emitted_by_tick", -1),
                    smell=d.get("smell", ""),
                    description=d.get("description", ""),
                    triggering_refs=d.get("triggering_refs") or {},
                    status="pending",
                )
                order.append(hid)
            elif etype == "status_change":
                rec = records.get(hid)
                if rec is None:
                    continue
                rec.status = d.get("new_status", rec.status)
                rec.history.append(
                    {
                        "ts": d.get("ts", ""),
                        "new_status": d.get("new_status", ""),
                        "by": d.get("by", ""),
                    }
                )
            # Unknown event types: ignored.

    return [records[h] for h in order]
