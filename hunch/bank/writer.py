"""Append-only writer for hunch_bank.jsonl.

Owns bank ID allocation (hb-NNNN, monotonic) and timestamp
monotonicity enforcement. Uses the same fcntl-locked append
helper as the rest of the journal layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hunch.journal.append import append_json_line


_BANK_ID_RE = re.compile(r"^hb-(\d+)$")


class BankWriter:
    """Append-only writer for the hunch bank event stream.

    Single-writer assumption (same as HunchesWriter). Concurrent
    writers would race on ID allocation; revisit if needed.
    """

    def __init__(self, bank_path: str | Path) -> None:
        self._bank_path = Path(bank_path)
        self._bank_path.parent.mkdir(parents=True, exist_ok=True)
        self._next_id_num = self._scan_max_id() + 1
        self._last_ts = self._scan_max_ts()

    def allocate_id(self) -> str:
        """Reserve the next hb-NNNN id."""
        bid = f"hb-{self._next_id_num:04d}"
        self._next_id_num += 1
        return bid

    def write_entry(
        self,
        bank_id: str,
        canonical_smell: str,
        canonical_description: str,
        source_run: str,
        source_hunch_id: str,
        ts: str,
    ) -> None:
        """Append an entry event for a new unique concern."""
        self._append({
            "type": "entry",
            "bank_id": bank_id,
            "canonical_smell": canonical_smell,
            "canonical_description": canonical_description,
            "source_run": source_run,
            "source_hunch_id": source_hunch_id,
            "ts": ts,
        })

    def write_link(
        self,
        bank_id: str,
        run: str,
        hunch_id: str,
        ts: str,
        *,
        judge_score: float | None = None,
        source: str = "ingest",
        replaces_bank_id: str | None = None,
    ) -> None:
        """Append a link event mapping a hunch to a bank entry."""
        self._append({
            "type": "link",
            "bank_id": bank_id,
            "run": run,
            "hunch_id": hunch_id,
            "judge_score": judge_score,
            "source": source,
            "replaces_bank_id": replaces_bank_id,
            "ts": ts,
        })

    def write_label(
        self,
        bank_id: str,
        run: str,
        hunch_id: str,
        label: str | None,
        ts: str,
        *,
        category: str = "",
        labeled_by: str = "",
    ) -> None:
        """Append a label event (human judgment or retraction).

        Pass label=None for a retraction.
        """
        self._append({
            "type": "label",
            "bank_id": bank_id,
            "run": run,
            "hunch_id": hunch_id,
            "label": label,
            "category": category,
            "labeled_by": labeled_by,
            "ts": ts,
        })

    def write_tombstone(
        self,
        run: str,
        ts: str,
        *,
        reason: str = "",
    ) -> None:
        """Append a tombstone event to drop a run."""
        self._append({
            "type": "tombstone",
            "run": run,
            "reason": reason,
            "ts": ts,
        })

    def _append(self, event: dict[str, Any]) -> None:
        ts = event.get("ts", "")
        if ts and self._last_ts and ts <= self._last_ts:
            raise RuntimeError(
                f"Timestamp monotonicity violation: new event ts={ts!r} "
                f"<= last ts={self._last_ts!r}. "
                f"Is the system clock jumping or another process writing?"
            )
        append_json_line(self._bank_path, event)
        if ts:
            self._last_ts = ts

    def _scan_max_id(self) -> int:
        if not self._bank_path.exists():
            return 0
        max_n = 0
        with open(self._bank_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bid = d.get("bank_id", "")
                m = _BANK_ID_RE.match(bid)
                if m:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
        return max_n

    def _scan_max_ts(self) -> str:
        if not self._bank_path.exists():
            return ""
        max_ts = ""
        with open(self._bank_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = d.get("ts", "")
                if ts and ts > max_ts:
                    max_ts = ts
        return max_ts
