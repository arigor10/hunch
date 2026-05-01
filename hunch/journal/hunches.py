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

META_NOTE = (
    "Hunch is an AI colleague that watches this conversation and the "
    "written artifacts in the background. It fires periodically based on "
    "a triggering policy and flags things that may not add up \u2014 anomalies, "
    "overlooked discrepancies, or patterns worth a second look. When the "
    "user approves a hunch, it is delivered to you via the UserPromptSubmit "
    "hook as a <hunch-injection> block on your next turn, and its status "
    "here changes to 'surfaced'. Hunches marked 'surfaced' have already "
    "been delivered to you and were likely addressed. You do not need to "
    "respond to them again unless you believe they warrant revisiting."
)


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
    bookmark_prev: int
    bookmark_now: int
    smell: str
    description: str
    triggering_refs: dict[str, list[str]]
    status: str
    history: list[dict[str, Any]] = field(default_factory=list)
    filtered: bool = False
    filter_applied: bool = False
    filter_type: str = ""
    filter_reason: str = ""
    duplicate_of: str | None = None


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
        self._ensure_meta_header()

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def _check_id_monotonicity(self, hunch_id: str) -> None:
        """Raise if the file already contains an ID >= the one we're writing."""
        m = _HUNCH_ID_RE.match(hunch_id)
        if not m:
            return
        new_num = int(m.group(1))
        current_max = self._scan_max_id()
        if current_max >= new_num:
            raise RuntimeError(
                f"Hunch ID monotonicity violation: about to write {hunch_id} "
                f"but file already contains h-{current_max:04d}. "
                f"Is another process writing to {self.hunches_path}?"
            )

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
        *,
        bookmark_prev: int,
        bookmark_now: int,
        filter_applied: bool = False,
    ) -> None:
        """Append an emit event for a freshly-minted hunch.

        The hunch's initial status is implicit: any reader folding
        events treats an emit as `status="pending"` until a later
        status_change event says otherwise.

        `bookmark_prev`/`bookmark_now` identify the exact replay-buffer
        window the Critic was evaluating. Recorded on emit (rather than
        recomputed later) so offline evaluators can pull the same
        dialogue slice the Critic "saw" — essential for novelty /
        duplicate-detection judges.

        `filter_applied` marks that this hunch went through the
        dedup + novelty filter and passed. Used by the retroactive
        filter to distinguish "checked and passed" from "never checked".
        """
        self._check_id_monotonicity(hunch_id)
        record = hunch_emit_record(
            hunch, hunch_id, ts, emitted_by_tick,
            bookmark_prev=bookmark_prev, bookmark_now=bookmark_now,
        )
        if filter_applied:
            record["filter_applied"] = True
        self._append(record)

    def write_filtered(
        self,
        hunch: Hunch,
        hunch_id: str,
        ts: str,
        emitted_by_tick: int,
        *,
        bookmark_prev: int,
        bookmark_now: int,
        filter_type: str,
        filter_reason: str,
        duplicate_of: str | None = None,
    ) -> None:
        """Append a filtered event — a hunch the filter suppressed.

        Same shape as an emit but with ``type: "filtered"`` and extra
        fields recording why.
        """
        self._check_id_monotonicity(hunch_id)
        record = hunch_emit_record(
            hunch, hunch_id, ts, emitted_by_tick,
            bookmark_prev=bookmark_prev, bookmark_now=bookmark_now,
        )
        record["type"] = "filtered"
        record["filter_type"] = filter_type
        record["filter_reason"] = filter_reason
        if duplicate_of:
            record["duplicate_of"] = duplicate_of
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

    def _ensure_meta_header(self) -> None:
        """Write the meta header as the first line if the file is new."""
        if self.hunches_path.exists() and self.hunches_path.stat().st_size > 0:
            return
        self._append({"type": "meta", "note": META_NOTE})

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

def read_current_hunches(
    hunches_path: str | Path,
    *,
    include_filtered: bool = False,
) -> list[HunchRecord]:
    """Read `hunches.jsonl` and return current-state records.

    Folds events in *file order* (which is append order, which — because
    the writer is single-threaded — is also timestamp order in v0).
    If the file doesn't exist yet, returns `[]`.

    When ``include_filtered`` is True, also returns hunches that were
    suppressed by the filter (dedup / novelty). These have
    ``filtered=True`` and carry ``filter_type``, ``filter_reason``, and
    optionally ``duplicate_of``.

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

            if etype == "emit" or (etype == "filtered" and include_filtered):
                if hid in records:
                    continue
                bp = d.get("bookmark_prev", -1)
                bn = d.get("bookmark_now", -1)
                if bp == -1 or bn == -1 or bn < bp:
                    bp = bn = -1
                is_filtered = etype == "filtered"
                records[hid] = HunchRecord(
                    hunch_id=hid,
                    emitted_ts=d.get("ts", ""),
                    emitted_by_tick=d.get("emitted_by_tick", -1),
                    bookmark_prev=bp,
                    bookmark_now=bn,
                    smell=d.get("smell", ""),
                    description=d.get("description", ""),
                    triggering_refs=d.get("triggering_refs") or {},
                    status="filtered" if is_filtered else "pending",
                    filtered=is_filtered,
                    filter_applied=bool(d.get("filter_applied")) or is_filtered,
                    filter_type=d.get("filter_type", "") if is_filtered else "",
                    filter_reason=d.get("filter_reason", "") if is_filtered else "",
                    duplicate_of=d.get("duplicate_of") if is_filtered else None,
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

    return [records[h] for h in order]
